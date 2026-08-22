"""Peer Radar: watch comparable artists and record who covers them.

Radar inverts discovery. Instead of only searching outlets by genre, it watches
up to twenty comparable artists and records which outlets are covering *them* —
premieres, interviews, reviews, playlist announcements. Every find is a
receipted coverage event backed by an evidence packet, and any event can be
turned into a campaign target whose evidence is that coverage.

Radar is read-only research. It reuses the existing search provider (quota
accounted), the existing fetcher (SSRF guards, robots, rate limits), the
existing page-state gate and platform-domain filter, and the existing durable
job runner. Nothing here can send a message; targeting an outlet enters the
normal qualification flow through the discovery pipeline.
"""

import re

from . import audit, clock, crypto, db, entities, evidence, extractor, fetcher, jobs, netguard, rbac, sanitizer
from .errors import FetchBlocked, ValidationError
from .providers import search as search_provider

RADAR_VERSION = "radar/1.0.0"

# Hard product limits, enforced in code and surfaced on the Radar screen.
MAX_WATCHED = 20
SWEEP_SEARCH_CAP = 60
SWEEP_STALE_DAYS = 7
QUERIES_PER_ARTIST = 3

SOURCE_PROFILE = "PROFILE"
SOURCE_USER = "USER"

# --- coverage kinds ---------------------------------------------------------
# Classified only from clear title/content signals. Anything ambiguous stays
# UNKNOWN — a coverage kind is a factual claim about what a page is, and Radar
# never guesses one.

ARTICLE = "ARTICLE"
PREMIERE = "PREMIERE"
INTERVIEW = "INTERVIEW"
PLAYLIST = "PLAYLIST"
KIND_UNKNOWN = "UNKNOWN"

COVERAGE_KINDS = [ARTICLE, PREMIERE, INTERVIEW, PLAYLIST, KIND_UNKNOWN]

# Ordered: the first matching rule wins, and the more specific coverage kinds
# are tested before the generic ARTICLE signals.
_KIND_RULES = [
    (PREMIERE, re.compile(r"\bpremieres?\b|\bpremiering\b", re.I)),
    (INTERVIEW, re.compile(r"\binterviews?\b|\bin\s+conversation\s+with\b|\bq\s*&\s*a\b", re.I)),
    (PLAYLIST, re.compile(r"\bplaylist\b|\badded\s+to\s+(?:our|the)\b", re.I)),
    (ARTICLE, re.compile(r"\breviews?\b|\bfeature[ds]?\b|\bprofile[ds]?\b|\bspotlights?\b", re.I)),
]


def classify_kind(title, text):
    """The coverage kind, from clear signals only. The page *title* is the
    strongest statement a page makes about itself, so it is tested first."""
    for kind, pattern in _KIND_RULES:
        if pattern.search(title or ""):
            return kind
    for kind, pattern in _KIND_RULES:
        if pattern.search(text or ""):
            return kind
    return KIND_UNKNOWN


# --------------------------------------------------------------------------
# watchlist
# --------------------------------------------------------------------------

def watched(tenant_id=None, active_only=False):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    sql = "SELECT * FROM watched_artist WHERE tenant_id = ?"
    if active_only:
        sql += " AND active = 1"
    sql += " ORDER BY created_at, rowid"
    return db.query(sql, (tenant_id,))


def active_count(tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM watched_artist WHERE tenant_id = ? AND active = 1",
        (tenant_id,),
    )
    return row["n"] if row else 0


def get_artist(artist_id):
    return db.query_one("SELECT * FROM watched_artist WHERE id = ?", (artist_id,))


def add_artist(name, source=SOURCE_USER, tenant_id=None):
    """Add an artist to the watchlist. The cap and the duplicate rule are both
    refused with a plain-language reason, never silently."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    name = " ".join((name or "").split())
    if not name:
        raise ValidationError("An artist name is required")
    if len(name) > 120:
        raise ValidationError("Artist names are capped at 120 characters")

    existing = db.query_one(
        "SELECT * FROM watched_artist WHERE tenant_id = ? AND name = ? COLLATE NOCASE",
        (tenant_id, name),
    )
    if existing is not None:
        if existing["active"]:
            raise ValidationError(f"{existing['name']} is already on the watchlist")
        if active_count(tenant_id) >= MAX_WATCHED:
            raise ValidationError(
                f"The watchlist is capped at {MAX_WATCHED} active artists. "
                "Deactivate one before adding another."
            )
        db.update("watched_artist", existing["id"], {"active": 1})
        audit.record("radar.artist_reactivated", entity_type="watched_artist",
                     entity_id=existing["id"], payload={"name": existing["name"]})
        return existing["id"]

    if active_count(tenant_id) >= MAX_WATCHED:
        raise ValidationError(
            f"The watchlist is capped at {MAX_WATCHED} active artists. "
            "Deactivate one before adding another."
        )
    artist_id = db.new_id("watch")
    db.insert("watched_artist", {
        "id": artist_id,
        "tenant_id": tenant_id,
        "name": name,
        "source": source,
        "active": 1,
        "created_at": clock.now_iso(),
    })
    audit.record("radar.artist_added", entity_type="watched_artist", entity_id=artist_id,
                 payload={"name": name, "source": source})
    return artist_id


def deactivate_artist(artist_id):
    row = get_artist(artist_id)
    if row is None:
        raise ValidationError("Unknown watched artist")
    db.update("watched_artist", artist_id, {"active": 0})
    audit.record("radar.artist_deactivated", entity_type="watched_artist",
                 entity_id=artist_id, payload={"name": row["name"]},
                 actor_kind=audit.ACTOR_USER)
    return artist_id


def seed_from_profiles(tenant_id=None):
    """First-visit seeding: the comparable artists the user already told REACH
    about become the initial watchlist, labelled PROFILE. Runs only while the
    watchlist has never held anything, so a deliberately emptied list is not
    silently refilled."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    if db.query_one("SELECT id FROM watched_artist WHERE tenant_id = ? LIMIT 1", (tenant_id,)):
        return []

    import json

    rows = db.query(
        "SELECT f.value_json FROM track_profile_field f "
        "JOIN track_profile p ON p.id = f.profile_id "
        "WHERE p.tenant_id = ? AND f.field = 'comparable_artists' "
        "ORDER BY p.created_at, f.generated_at",
        (tenant_id,),
    )
    seen = set()
    added = []
    for row in rows:
        try:
            names = json.loads(row["value_json"]) if row["value_json"] else []
        except (TypeError, ValueError):
            continue
        for name in names or []:
            cleaned = " ".join(str(name).split())
            if not cleaned or cleaned.lower() in seen:
                continue
            seen.add(cleaned.lower())
            try:
                add_artist(cleaned, source=SOURCE_PROFILE, tenant_id=tenant_id)
            except ValidationError:
                return added  # the cap is a hard stop, not an error to hide
            added.append(cleaned)
    return added


# --------------------------------------------------------------------------
# sweep state
# --------------------------------------------------------------------------

def state(tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    row = db.query_one("SELECT * FROM radar_state WHERE tenant_id = ?", (tenant_id,))
    if row is None:
        return {"tenant_id": tenant_id, "last_sweep_started_at": None,
                "last_sweep_finished_at": None, "searches_used": 0}
    return dict(row)


def _set_state(tenant_id, **fields):
    existing = db.query_one("SELECT tenant_id FROM radar_state WHERE tenant_id = ?",
                            (tenant_id,))
    fields["updated_at"] = clock.now_iso()
    if existing is None:
        payload = {"tenant_id": tenant_id, "last_sweep_started_at": None,
                   "last_sweep_finished_at": None, "searches_used": 0}
        payload.update(fields)
        db.insert("radar_state", payload)
    else:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        db.execute(f"UPDATE radar_state SET {assignments} WHERE tenant_id = ?",
                   tuple(fields.values()) + (tenant_id,))


def _spend_search(tenant_id):
    db.execute(
        "UPDATE radar_state SET searches_used = searches_used + 1, updated_at = ? "
        "WHERE tenant_id = ?",
        (clock.now_iso(), tenant_id),
    )


def sweep_due(tenant_id=None):
    """True when a sweep has never finished, or finished more than
    SWEEP_STALE_DAYS ago."""
    current = state(tenant_id)
    finished = current["last_sweep_finished_at"]
    if not finished:
        return True
    days = clock.days_since(finished)
    return days is None or days > SWEEP_STALE_DAYS


def pending_jobs(tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM job_run WHERE tenant_id = ? AND kind LIKE 'RADAR%' "
        "AND status IN (?, ?)",
        (tenant_id, jobs.PENDING, jobs.RUNNING),
    )
    return row["n"] if row else 0


# --------------------------------------------------------------------------
# sweep
# --------------------------------------------------------------------------

def plan_artist_queries(name):
    """Three queries per watched artist, no more. Coverage phrasing, not
    submission phrasing — Radar asks who wrote about the artist."""
    return [
        f'"{name}" premiere OR review',
        f'"{name}" interview OR feature',
        f'"{name}" playlist OR "added to"',
    ]


def start_sweep(tenant_id=None):
    """Enqueue a sweep as durable jobs. Draining happens in bounded chunks via
    :func:`run_to_completion`, exactly like discovery."""
    rbac.require("campaign.run_discovery")
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    job_id = jobs.enqueue(
        "RADAR_SWEEP", {"radar_version": RADAR_VERSION},
        idempotency_key=f"radar:sweep:{tenant_id}:{clock.now_iso()}",
        tenant_id=tenant_id,
    )
    audit.record("radar.sweep_started", entity_type="job_run", entity_id=job_id,
                 actor_kind=audit.ACTOR_USER)
    return job_id


@jobs.register("RADAR_SWEEP")
def _radar_sweep(context):
    if context.cancelled():
        return None
    tenant_id = context.tenant_id
    artists = watched(tenant_id, active_only=True)
    _set_state(tenant_id, last_sweep_started_at=clock.now_iso(), searches_used=0)

    queries = 0
    for artist in artists:
        for query in plan_artist_queries(artist["name"]):
            context.enqueue("RADAR_SEARCH", {
                "artist_id": artist["id"],
                "artist_name": artist["name"],
                "query": query,
                "sweep_id": context.id,
            }, idempotency_key=f"radar:search:{context.id}:{query}")
            queries += 1
    context.progress(0, queries, "Sweeping coverage of watched artists")
    audit.record("radar.sweep_planned", entity_type="job_run", entity_id=context.id,
                 payload={"artists": len(artists), "queries": queries})
    return {"artists": len(artists), "queries": queries, "search_cap": SWEEP_SEARCH_CAP}


@jobs.register("RADAR_SEARCH")
def _radar_search(context):
    if context.cancelled():
        return None
    tenant_id = context.tenant_id
    used = state(tenant_id)["searches_used"]
    if used >= SWEEP_SEARCH_CAP:
        # The cap is a stop rule, not a failure: the skip is recorded so the
        # sweep result states plainly that the budget ran out.
        return {"skipped": "SEARCH_CAP", "searches_used": used, "cap": SWEEP_SEARCH_CAP}

    query = context.payload["query"]
    response = search_provider.search(query, limit=8)
    _spend_search(tenant_id)
    context.cost(searches=1)

    enqueued = 0
    platform_skipped = 0
    for result in response.items:
        url = result.get("url")
        if not url:
            continue
        try:
            validated = netguard.validate_url(url, resolve=False)
        except FetchBlocked:
            continue
        if entities.is_platform_domain(validated["domain"]):
            # Coverage lives on the open web. A platform page is never a
            # coverage source, exactly as it is never an outlet.
            platform_skipped += 1
            continue
        context.enqueue("RADAR_FETCH", {
            "url": url,
            "artist_id": context.payload["artist_id"],
            "artist_name": context.payload["artist_name"],
            "sweep_id": context.payload.get("sweep_id"),
        }, idempotency_key=(
            f"radar:fetch:{context.payload.get('sweep_id')}:"
            f"{context.payload['artist_id']}:{url}"
        ))
        enqueued += 1

    return {
        "query": query,
        "results": len(response.items),
        "fetch_jobs": enqueued,
        "platform_skipped": platform_skipped,
        "mode": response.mode,
        "note": response.note,
    }


def _mention_excerpt(text, artist_name, width=200):
    """The passage where the page actually mentions the artist, or None.

    No mention, no event: an excerpt is the receipt for "this outlet covered
    this artist", and Radar does not fabricate relevance from a search hit.
    """
    if not text or not artist_name:
        return None
    match = re.search(re.escape(artist_name), text, re.I)
    if match is None:
        return None
    start = max(0, match.start() - width // 2)
    end = min(len(text), match.end() + width // 2)
    return (("…" if start > 0 else "")
            + " ".join(text[start:end].split())
            + ("…" if end < len(text) else ""))


def coverage_dedup_key(artist_id, url):
    """One event per (watched artist, canonical URL)."""
    return crypto.url_hash(f"{artist_id}|{url}")


@jobs.register("RADAR_FETCH")
def _radar_fetch(context):
    if context.cancelled():
        return None
    tenant_id = context.tenant_id
    url = context.payload["url"]
    artist_id = context.payload["artist_id"]
    artist_name = context.payload["artist_name"]

    try:
        result = fetcher.fetch(url)
    except FetchBlocked as exc:
        evidence.record_source_document(
            url=url, domain=netguard.registrable_domain(_hostname(url)),
            provider="open_web_fetch", fetch_status=evidence.FETCH_BLOCKED,
            robots_decision="N/A", block_reason=exc.reason, tenant_id=tenant_id,
        )
        return {"blocked": exc.reason, "url": url}

    context.cost(pages=1)
    sanitized = sanitizer.sanitize(result.text(), base_url=result.final_url)
    page_state = extractor.page_state(sanitized, http_status=result.status)

    document_id = evidence.record_source_document(
        url=result.final_url, domain=result.domain, provider="open_web_fetch",
        fetch_status=evidence.FETCH_OK, http_status=result.status, mime=result.mime,
        byte_count=result.bytes, sanitized_text=sanitized["visible_text"],
        title=sanitized["title"], robots_decision=result.robots_decision,
        tenant_id=tenant_id,
    )

    if page_state != extractor.PAGE_OK:
        # An interstitial or an error page is not the site behind it: nothing
        # on it is evidence that anyone covered anyone.
        audit.record("radar.page_unusable", entity_type="source_document",
                     entity_id=document_id,
                     payload={"url": result.final_url, "page_state": page_state})
        return {"url": result.final_url, "page_state": page_state, "skipped": True}

    if entities.is_platform_domain(result.domain):
        # Belt to the search-stage filter: a redirect can land on a platform.
        return {"skipped": "PLATFORM_DOMAIN", "domain": result.domain}

    excerpt = _mention_excerpt(sanitized["visible_text"], artist_name)
    if excerpt is None:
        return {"skipped": "NO_ARTIST_MENTION", "url": result.final_url}

    title = sanitized["title"]
    kind = classify_kind(title, sanitized["visible_text"])
    dedup = coverage_dedup_key(artist_id, result.final_url)

    existing = db.query_one(
        "SELECT id FROM coverage_event WHERE tenant_id = ? AND dedup_key = ?",
        (tenant_id, dedup),
    )
    if existing is not None:
        db.update("coverage_event", existing["id"], {"retrieved_at": clock.now_iso()})
        return {"event_id": existing["id"], "deduped": True, "url": result.final_url}

    event_id = db.new_id("cov")
    evidence_id = evidence.record(
        entity_type="coverage_event", entity_id=event_id, supports_field="coverage",
        value={"artist": artist_name, "kind": kind, "title": title},
        source_url=result.final_url, source_domain=result.domain,
        source_type="OFFICIAL", excerpt=excerpt,
        confidence=0.7 if kind != KIND_UNKNOWN else 0.5,
        extractor_version=RADAR_VERSION, source_document_id=document_id,
        tenant_id=tenant_id,
    )
    db.insert("coverage_event", {
        "id": event_id,
        "tenant_id": tenant_id,
        "watched_artist_id": artist_id,
        "outlet_id": None,
        "url": result.final_url,
        "domain": result.domain,
        "title": title,
        "kind": kind,
        "excerpt": excerpt,
        "evidence_id": evidence_id,
        "retrieved_at": clock.now_iso(),
        "dedup_key": dedup,
        "targeted_target_id": None,
        "created_at": clock.now_iso(),
    })
    audit.record("radar.coverage_recorded", entity_type="coverage_event",
                 entity_id=event_id,
                 payload={"artist": artist_name, "domain": result.domain, "kind": kind})
    return {"event_id": event_id, "kind": kind, "url": result.final_url}


def _hostname(url):
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or ""


def run_to_completion(max_jobs=500, max_seconds=None, tenant_id=None):
    """Drain radar jobs in a bounded chunk, exactly like discovery: each web
    request does at most ~20 seconds of work and the page keeps calling until
    nothing is pending."""
    import time

    tenant_id = tenant_id or rbac.current_principal().tenant_id
    jobs.requeue_stale()
    started = time.monotonic()
    processed = 0
    while processed < max_jobs and pending_jobs(tenant_id):
        if max_seconds is not None and time.monotonic() - started >= max_seconds:
            break
        job = jobs.run_one()
        if job is None:
            break
        processed += 1
    _maybe_finish(tenant_id)
    return processed


def _maybe_finish(tenant_id):
    """Stamp the sweep finished once no radar work remains."""
    if pending_jobs(tenant_id):
        return
    current = state(tenant_id)
    started = current["last_sweep_started_at"]
    finished = current["last_sweep_finished_at"]
    if started and (not finished or finished < started):
        _set_state(tenant_id, last_sweep_finished_at=clock.now_iso())
        audit.record("radar.sweep_finished", entity_type="radar_state", entity_id=tenant_id,
                     payload={"searches_used": state(tenant_id)["searches_used"],
                              "search_cap": SWEEP_SEARCH_CAP})


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------

def events(tenant_id=None, limit=200):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    return db.query(
        "SELECT ce.*, w.name AS artist_name, o.name AS outlet_name, "
        "t.campaign_id AS targeted_campaign_id "
        "FROM coverage_event ce "
        "JOIN watched_artist w ON w.id = ce.watched_artist_id "
        "LEFT JOIN outlet o ON o.id = ce.outlet_id "
        "LEFT JOIN campaign_target t ON t.id = ce.targeted_target_id "
        "WHERE ce.tenant_id = ? "
        "ORDER BY ce.retrieved_at DESC, ce.rowid DESC LIMIT ?",
        (tenant_id, limit),
    )


def get_event(event_id):
    return db.query_one(
        "SELECT ce.*, w.name AS artist_name FROM coverage_event ce "
        "JOIN watched_artist w ON w.id = ce.watched_artist_id WHERE ce.id = ?",
        (event_id,),
    )


# --------------------------------------------------------------------------
# targeting
# --------------------------------------------------------------------------

def target_event(event_id, campaign_id):
    """Turn a coverage event into a campaign target — through the normal
    pipeline. The coverage page is fetched and resolved by the same jobs
    discovery uses, so scoring, risk and compliance are never skipped, and the
    coverage itself is attached to the outlet's evidence."""
    from . import campaigns, pipeline

    rbac.require("campaign.run_discovery")
    event = get_event(event_id)
    if event is None:
        raise ValidationError("Unknown coverage event")
    campaign = campaigns.get(campaign_id)
    if campaign is None:
        raise ValidationError("Unknown campaign")
    if campaign["status"] in (campaigns.COMPLETED, campaigns.CANCELLED):
        raise ValidationError("That campaign is finished — pick an active one")

    if event["targeted_target_id"]:
        existing = campaigns.get_target(event["targeted_target_id"])
        if existing is not None:
            return {"target_id": existing["id"], "outlet_id": existing["outlet_id"],
                    "campaign_id": existing["campaign_id"], "status": existing["status"],
                    "already_targeted": True}

    jobs.enqueue("FETCH_PUBLIC_PAGE", {
        "url": event["url"],
        "query": f'radar coverage of {event["artist_name"]}',
        "family": "RADAR",
    }, idempotency_key=f"fetch:{campaign_id}:{event['url']}", campaign_id=campaign_id)
    pipeline.run_to_completion(campaign_id, max_seconds=20)

    outlet = db.query_one(
        "SELECT * FROM outlet WHERE tenant_id = ? AND url = ?",
        (event["tenant_id"], event["url"]),
    ) or entities.outlet_by_domain(event["domain"], tenant_id=event["tenant_id"])
    target = None
    if outlet is not None:
        target = db.query_one(
            "SELECT * FROM campaign_target WHERE campaign_id = ? AND outlet_id = ?",
            (campaign_id, outlet["id"]),
        )
    if outlet is None or target is None:
        raise ValidationError(
            "The coverage page could not be qualified into a target — the fetch "
            "was refused or the page was unusable. Nothing was invented in its place."
        )

    evidence.record(
        entity_type="outlet", entity_id=outlet["id"], supports_field="peer_coverage",
        value={"artist": event["artist_name"], "title": event["title"],
               "kind": event["kind"]},
        source_url=event["url"], source_domain=event["domain"], source_type="OFFICIAL",
        excerpt=event["excerpt"], confidence=0.7, extractor_version=RADAR_VERSION,
        tenant_id=event["tenant_id"],
    )
    db.update("coverage_event", event_id, {
        "outlet_id": outlet["id"],
        "targeted_target_id": target["id"],
    })
    audit.record("radar.targeted", entity_type="campaign_target", entity_id=target["id"],
                 payload={"event_id": event_id, "campaign_id": campaign_id},
                 actor_kind=audit.ACTOR_USER)
    refreshed = campaigns.get_target(target["id"])
    return {"target_id": target["id"], "outlet_id": outlet["id"],
            "campaign_id": campaign_id, "status": refreshed["status"],
            "already_targeted": False}
