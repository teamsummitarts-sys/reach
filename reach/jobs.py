"""Durable job system.

The host application has no queue, so REACH provides one over the same SQLite
store: jobs are rows, and a worker thread claims them. That keeps discovery out
of the request cycle and makes progress, cancellation and retries observable.

Every job is idempotent by key, retries with exponential backoff, can be
cancelled mid-flight, respects provider kill switches, and records its cost.
Tests drain the queue synchronously via :func:`run_pending`, so job behaviour is
tested directly rather than through timing.
"""

import json
import threading
import traceback

from . import audit, clock, db, policy, rbac
from .errors import KillSwitchEngaged, ProviderRateLimited, ReachError

PENDING = "PENDING"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
DEAD_LETTER = "DEAD_LETTER"
CANCELLED = "CANCELLED"

JOB_KINDS = [
    "BUILD_TRACK_PROFILE", "PLAN_DISCOVERY", "SEARCH_PROVIDER", "FETCH_PUBLIC_PAGE",
    "SANITIZE_CONTENT", "EXTRACT_SOURCE_DATA", "RESOLVE_ENTITY", "RESOLVE_ORGANIZATION",
    "RESOLVE_CONTACT", "VERIFY_SUBMISSION_ROUTE", "DEDUPLICATE", "SCORE_OPPORTUNITY",
    "ASSESS_RISK", "ASSESS_COMPLIANCE", "GENERATE_PITCH", "REQUEST_APPROVAL",
    "QUEUE_EMAIL", "SEND_EMAIL", "PROCESS_BOUNCE", "PROCESS_REPLY", "CREATE_HUMAN_TASK",
    "CHECK_PLACEMENT", "REFRESH_SOURCE", "UPDATE_ANALYTICS", "RUN_DISCOVERY",
]

_handlers = {}
_worker = None
_worker_lock = threading.Lock()
_stop_event = threading.Event()

# Provider-specific concurrency ceilings. One inflight request per provider is
# the conservative Phase One default; discovery is I/O bound on politeness, not
# on parallelism.
PROVIDER_CONCURRENCY = {"open_web_fetch": 2, "open_web_search": 1, "youtube": 1}


def register(kind):
    def decorator(function):
        _handlers[kind] = function
        return function
    return decorator


def handler_for(kind):
    return _handlers.get(kind)


# --------------------------------------------------------------------------
# enqueue
# --------------------------------------------------------------------------

def enqueue(kind, payload=None, idempotency_key=None, campaign_id=None,
            max_attempts=3, total=0, tenant_id=None):
    """Insert a job. A repeated idempotency key returns the existing job id."""
    if kind not in JOB_KINDS:
        raise ReachError(f"Unknown job kind: {kind}")
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    payload = payload or {}
    key = idempotency_key or f"{kind}:{campaign_id}:{json.dumps(payload, sort_keys=True)}"

    existing = db.query_one("SELECT id FROM job_run WHERE idempotency_key = ?", (key,))
    if existing is not None:
        return existing["id"]

    job_id = db.new_id("job")
    db.insert("job_run", {
        "id": job_id,
        "tenant_id": tenant_id,
        "campaign_id": campaign_id,
        "kind": kind,
        "payload_json": json.dumps(payload, sort_keys=True),
        "idempotency_key": key,
        "status": PENDING,
        "attempts": 0,
        "max_attempts": max_attempts,
        "progress": 0,
        "total": total,
        "message": None,
        "error": None,
        "error_kind": None,
        "result_json": None,
        "cost_json": json.dumps({}),
        "cancelled": 0,
        "next_attempt_at": clock.now_iso(),
        "created_at": clock.now_iso(),
        "started_at": None,
        "finished_at": None,
    })
    return job_id


def get(job_id):
    return db.query_one("SELECT * FROM job_run WHERE id = ?", (job_id,))


def for_campaign(campaign_id, limit=50):
    return db.query(
        "SELECT * FROM job_run WHERE campaign_id = ? ORDER BY created_at DESC LIMIT ?",
        (campaign_id, limit),
    )


def cancel(job_id):
    rbac.require("campaign.cancel")
    job = get(job_id)
    if job is None:
        return False
    db.update("job_run", job_id, {"cancelled": 1})
    if job["status"] == PENDING:
        db.update("job_run", job_id, {"status": CANCELLED,
                                      "finished_at": clock.now_iso()})
    audit.record("job.cancelled", entity_type="job_run", entity_id=job_id,
                 actor_kind=audit.ACTOR_USER)
    return True


def cancel_campaign_jobs(campaign_id):
    rbac.require("campaign.cancel")
    rows = db.query(
        "SELECT id FROM job_run WHERE campaign_id = ? AND status IN (?, ?)",
        (campaign_id, PENDING, RUNNING),
    )
    for row in rows:
        db.update("job_run", row["id"], {"cancelled": 1})
        db.execute(
            "UPDATE job_run SET status = ?, finished_at = ? WHERE id = ? AND status = ?",
            (CANCELLED, clock.now_iso(), row["id"], PENDING),
        )
    return len(rows)


def is_cancelled(job_id):
    row = db.query_one("SELECT cancelled FROM job_run WHERE id = ?", (job_id,))
    return bool(row and row["cancelled"])


def set_progress(job_id, progress, total=None, message=None):
    payload = {"progress": progress}
    if total is not None:
        payload["total"] = total
    if message is not None:
        payload["message"] = message
    db.update("job_run", job_id, payload)


def add_cost(job_id, **costs):
    job = get(job_id)
    if job is None:
        return
    current = json.loads(job["cost_json"] or "{}")
    for key, value in costs.items():
        current[key] = current.get(key, 0) + value
    db.update("job_run", job_id, {"cost_json": json.dumps(current)})


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

class JobContext:
    """What a handler is given. Deliberately narrow: a handler cannot enqueue
    outside its campaign or reach the principal's credentials."""

    def __init__(self, job):
        self.job = job
        self.id = job["id"]
        self.kind = job["kind"]
        self.campaign_id = job["campaign_id"]
        self.tenant_id = job["tenant_id"]
        self.payload = json.loads(job["payload_json"] or "{}")

    def cancelled(self):
        return is_cancelled(self.id)

    def progress(self, done, total=None, message=None):
        set_progress(self.id, done, total, message)

    def cost(self, **costs):
        add_cost(self.id, **costs)

    def enqueue(self, kind, payload=None, idempotency_key=None, max_attempts=3):
        return enqueue(kind, payload, idempotency_key, campaign_id=self.campaign_id,
                       max_attempts=max_attempts, tenant_id=self.tenant_id)


def _claim_next():
    """Claim one runnable job. SQLite's write lock makes this atomic enough for
    the single-process worker Phase One runs."""
    now = clock.now_iso()
    row = db.query_one(
        "SELECT * FROM job_run WHERE status = ? AND cancelled = 0 "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
        "ORDER BY created_at LIMIT 1",
        (PENDING, now),
    )
    if row is None:
        return None
    cursor = db.execute(
        "UPDATE job_run SET status = ?, started_at = ?, attempts = attempts + 1 "
        "WHERE id = ? AND status = ?",
        (RUNNING, now, row["id"], PENDING),
    )
    if cursor.rowcount == 0:
        return None
    return get(row["id"])


def run_one():
    """Execute at most one job. Returns the job row, or None if idle."""
    job = _claim_next()
    if job is None:
        return None

    context = JobContext(job)
    handler = handler_for(job["kind"])

    if handler is None:
        _fail(job, f"No handler registered for {job['kind']}", "NO_HANDLER", retry=False)
        return get(job["id"])

    try:
        if policy.global_stop_engaged():
            raise KillSwitchEngaged("Global emergency stop is engaged")
        result = handler(context)
        if context.cancelled():
            db.update("job_run", job["id"], {
                "status": CANCELLED, "finished_at": clock.now_iso(),
            })
        else:
            db.update("job_run", job["id"], {
                "status": SUCCEEDED,
                "finished_at": clock.now_iso(),
                "result_json": json.dumps(result, default=str) if result is not None else None,
                "error": None,
                "error_kind": None,
            })
            audit.record("job.succeeded", entity_type="job_run", entity_id=job["id"],
                         payload={"kind": job["kind"]})
    except ProviderRateLimited as exc:
        delay = exc.retry_after or _backoff(job["attempts"])
        _fail(job, str(exc), exc.kind, retry=True, delay_seconds=delay)
    except ReachError as exc:
        _fail(job, str(exc), exc.kind, retry=exc.retryable)
    except Exception as exc:  # pragma: no cover - unexpected handler failure
        _fail(job, f"{exc}\n{traceback.format_exc(limit=3)}", "UNEXPECTED", retry=True)
    return get(job["id"])


def _backoff(attempts):
    return min(300, 2 ** max(0, attempts))


def _fail(job, message, error_kind, retry=True, delay_seconds=None):
    attempts = job["attempts"]
    can_retry = retry and attempts < job["max_attempts"]
    status = PENDING if can_retry else (DEAD_LETTER if retry else FAILED)
    payload = {
        "status": status,
        "error": message[:2000],
        "error_kind": error_kind,
        "finished_at": None if can_retry else clock.now_iso(),
    }
    if can_retry:
        payload["next_attempt_at"] = clock.seconds_from_now(
            delay_seconds if delay_seconds is not None else _backoff(attempts)
        )
    db.update("job_run", job["id"], payload)
    audit.record("job.failed", entity_type="job_run", entity_id=job["id"],
                 payload={"kind": job["kind"], "error_kind": error_kind,
                          "status": status, "attempt": attempts})


def requeue_stale(older_than_seconds=300):
    """Requeue jobs a dead worker left claimed forever.

    A SIGKILLed request (the server's timeout, a deploy, a crash) never reaches
    the code that marks its in-flight job finished, so the job sits in RUNNING
    where nothing will ever claim it again. Anything RUNNING longer than the
    threshold is returned to PENDING; the attempt was already counted at claim
    time, so a job that keeps killing its worker still dead-letters after
    max_attempts rather than looping forever.
    """
    from datetime import timedelta

    cutoff = clock.to_iso(clock.now() - timedelta(seconds=older_than_seconds))
    cursor = db.execute(
        "UPDATE job_run SET status = ?, next_attempt_at = NULL "
        "WHERE status = ? AND started_at < ? AND cancelled = 0",
        (PENDING, RUNNING, cutoff),
    )
    if cursor.rowcount:
        audit.record("jobs.stale_requeued", entity_type="job_run", entity_id=None,
                     payload={"count": cursor.rowcount,
                              "older_than_seconds": older_than_seconds})
    return cursor.rowcount


def run_pending(limit=200):
    """Drain the queue. Used by tests and by the synchronous 'run now' path."""
    processed = []
    for _ in range(limit):
        job = run_one()
        if job is None:
            break
        processed.append(job)
    return processed


def pending_count(campaign_id=None):
    if campaign_id:
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM job_run WHERE campaign_id = ? AND status IN (?, ?)",
            (campaign_id, PENDING, RUNNING),
        )
    else:
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM job_run WHERE status IN (?, ?)", (PENDING, RUNNING)
        )
    return row["n"] if row else 0


def dead_letters(tenant_id=None, limit=50):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    return db.query(
        "SELECT * FROM job_run WHERE tenant_id = ? AND status = ? "
        "ORDER BY finished_at DESC LIMIT ?",
        (tenant_id, DEAD_LETTER, limit),
    )


def progress_for_campaign(campaign_id):
    rows = for_campaign(campaign_id, limit=200)
    total = len(rows)
    done = sum(1 for row in rows if row["status"] in (SUCCEEDED, CANCELLED))
    failed = sum(1 for row in rows if row["status"] in (FAILED, DEAD_LETTER))
    running = sum(1 for row in rows if row["status"] == RUNNING)
    pending = sum(1 for row in rows if row["status"] == PENDING)
    active = [dict(row) for row in rows if row["status"] in (RUNNING, PENDING)][:10]
    return {
        "total": total,
        "done": done,
        "failed": failed,
        "running": running,
        "pending": pending,
        "pct": round(done / total * 100) if total else 0,
        "active": active,
        "complete": pending == 0 and running == 0,
    }


# --------------------------------------------------------------------------
# background worker
# --------------------------------------------------------------------------

def _worker_loop(poll_seconds):
    while not _stop_event.is_set():
        try:
            job = run_one()
        except Exception:  # pragma: no cover - the worker must never die
            job = None
        if job is None:
            _stop_event.wait(poll_seconds)


def start_worker(poll_seconds=1.0):
    """Start the background worker. Idempotent; a no-op under tests, which
    drain the queue explicitly."""
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return _worker
        _stop_event.clear()
        _worker = threading.Thread(target=_worker_loop, args=(poll_seconds,),
                                   name="reach-jobs", daemon=True)
        _worker.start()
        return _worker


def stop_worker():
    global _worker
    _stop_event.set()
    with _worker_lock:
        worker = _worker
        _worker = None
    if worker is not None and worker.is_alive():  # pragma: no cover - timing
        worker.join(timeout=5)
