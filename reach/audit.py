"""Append-only audit log.

Each event carries the hash of the event before it, so a deleted or edited row
breaks the chain and :func:`verify_chain` reports where. Every external action
(fetch, provider call, send, approval, policy change) writes one.
"""

import hashlib
import json

from . import clock, db

ACTOR_USER = "USER"
ACTOR_SYSTEM = "SYSTEM"
ACTOR_AGENT = "AGENT"


def _canonical(payload):
    return json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)


def record(action, entity_type=None, entity_id=None, payload=None,
           actor_kind=ACTOR_SYSTEM, actor_id=None, tenant_id=None):
    from . import rbac

    if tenant_id is None or actor_id is None:
        try:
            principal = rbac.current_principal()
            tenant_id = tenant_id or principal.tenant_id
            if actor_id is None and actor_kind == ACTOR_USER:
                actor_id = principal.id
        except Exception:  # pragma: no cover - audit must never break a caller
            tenant_id = tenant_id or rbac.DEFAULT_TENANT_ID

    conn = db.connect()
    row = conn.execute(
        "SELECT hash, seq FROM audit_event WHERE tenant_id IS ? ORDER BY seq DESC LIMIT 1",
        (tenant_id,),
    ).fetchone()
    prev_hash = row["hash"] if row else None
    seq = (row["seq"] + 1) if row else 1

    created_at = clock.now_iso()
    payload_json = _canonical(payload)
    digest = hashlib.sha256(
        "|".join([
            prev_hash or "",
            str(seq),
            tenant_id or "",
            actor_kind,
            actor_id or "",
            action,
            entity_type or "",
            entity_id or "",
            payload_json,
            created_at,
        ]).encode("utf-8")
    ).hexdigest()

    event_id = db.new_id("aud")
    db.insert("audit_event", {
        "id": event_id,
        "seq": seq,
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "actor_kind": actor_kind,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "payload_json": payload_json,
        "created_at": created_at,
        "prev_hash": prev_hash,
        "hash": digest,
    })
    return event_id


def events(tenant_id=None, limit=100, action=None, entity_id=None):
    from . import rbac

    tenant_id = tenant_id or rbac.current_principal().tenant_id
    sql = "SELECT * FROM audit_event WHERE tenant_id IS ?"
    params = [tenant_id]
    if action:
        sql += " AND action = ?"
        params.append(action)
    if entity_id:
        sql += " AND entity_id = ?"
        params.append(entity_id)
    sql += " ORDER BY seq DESC LIMIT ?"
    params.append(limit)
    return db.query(sql, tuple(params))


def verify_chain(tenant_id=None):
    """Recompute every hash. Returns (ok, first_broken_seq_or_None)."""
    from . import rbac

    tenant_id = tenant_id or rbac.current_principal().tenant_id
    prev_hash = None
    rows = db.query(
        "SELECT * FROM audit_event WHERE tenant_id IS ? ORDER BY seq ASC", (tenant_id,)
    )
    for row in rows:
        digest = hashlib.sha256(
            "|".join([
                prev_hash or "",
                str(row["seq"]),
                row["tenant_id"] or "",
                row["actor_kind"],
                row["actor_id"] or "",
                row["action"],
                row["entity_type"] or "",
                row["entity_id"] or "",
                row["payload_json"] or "{}",
                row["created_at"],
            ]).encode("utf-8")
        ).hexdigest()
        if digest != row["hash"] or (row["prev_hash"] or None) != prev_hash:
            return False, row["seq"]
        prev_hash = row["hash"]
    return True, None
