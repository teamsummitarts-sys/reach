"""Evidence and provenance.

Every meaningful claim REACH stores points at an evidence packet: the source,
the excerpt that justified it, when it was retrieved and when it goes stale.
The "Why do we know this?" panel reads straight from these rows.

If there is no evidence, the field stays UNKNOWN. Nothing is invented.
"""

import json

from . import clock, crypto, db, firewall, rbac

DEFAULT_STALE_DAYS = 45


def record(entity_type, entity_id, supports_field, value, source_url, source_domain,
           source_type, excerpt=None, confidence=0.5, extractor_version="extractor/1.0.0",
           content_hash=None, source_document_id=None, policy_decision_id=None,
           stale_days=DEFAULT_STALE_DAYS, verified_at=None, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    evidence_id = db.new_id("ev")
    db.insert("evidence_packet", {
        "id": evidence_id,
        "tenant_id": tenant_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "source_url": source_url,
        "source_domain": source_domain,
        "source_type": source_type,
        "excerpt": firewall.excerpt(excerpt) if excerpt else None,
        "content_hash": content_hash,
        "supports_field": supports_field,
        "extracted_value_json": json.dumps(value, default=str),
        "confidence": confidence,
        "retrieved_at": clock.now_iso(),
        "verified_at": verified_at,
        "stale_at": clock.days_from_now(stale_days),
        "extractor_version": extractor_version,
        "policy_decision_id": policy_decision_id,
        "source_document_id": source_document_id,
    })
    return evidence_id


def for_entity(entity_type, entity_id, field=None):
    sql = "SELECT * FROM evidence_packet WHERE entity_type = ? AND entity_id = ?"
    params = [entity_type, entity_id]
    if field:
        sql += " AND supports_field = ?"
        params.append(field)
    sql += " ORDER BY retrieved_at DESC"
    return db.query(sql, tuple(params))


def get(evidence_id):
    return db.query_one("SELECT * FROM evidence_packet WHERE id = ?", (evidence_id,))


def mark_verified(evidence_id, stale_days=DEFAULT_STALE_DAYS):
    db.update("evidence_packet", evidence_id, {
        "verified_at": clock.now_iso(),
        "stale_at": clock.days_from_now(stale_days),
    })


def is_stale(row):
    return clock.is_past(row["stale_at"])


def corroboration(entity_type, entity_id, field):
    """How many *independent domains* support this field."""
    rows = for_entity(entity_type, entity_id, field)
    domains = {row["source_domain"] for row in rows if row["source_domain"]}
    return len(domains)


def summary(entity_type, entity_id):
    """What the "Why do we know this?" panel renders."""
    rows = for_entity(entity_type, entity_id)
    items = []
    for row in rows:
        try:
            value = json.loads(row["extracted_value_json"]) if row["extracted_value_json"] else None
        except (TypeError, ValueError):
            value = None
        items.append({
            "id": row["id"],
            "field": row["supports_field"],
            "value": value,
            "source_url": row["source_url"],
            "source_domain": row["source_domain"],
            "source_type": row["source_type"],
            "excerpt": row["excerpt"],
            "confidence": row["confidence"],
            "retrieved_at": row["retrieved_at"],
            "verified_at": row["verified_at"],
            "stale": is_stale(row),
            "official": row["source_type"] == "OFFICIAL",
            "corroborating_domains": corroboration(entity_type, entity_id, row["supports_field"]),
            "extractor_version": row["extractor_version"],
        })
    return items


# --------------------------------------------------------------------------
# source documents
# --------------------------------------------------------------------------

FETCH_OK = "OK"
FETCH_BLOCKED = "BLOCKED"
FETCH_ERROR = "ERROR"


def record_source_document(url, domain, provider, fetch_status, campaign_id=None,
                           http_status=None, mime=None, byte_count=None,
                           sanitized_text=None, title=None, classification=None,
                           robots_decision="UNKNOWN", block_reason=None,
                           stale_days=DEFAULT_STALE_DAYS, tenant_id=None):
    """One row per retrieval attempt, successful or refused.

    Only a bounded excerpt of page text is retained — enough to justify a claim,
    never a full copy of a third-party page. Providers whose policy forbids raw
    storage get no text at all.
    """
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    stored_text = None
    if sanitized_text and firewall.source_usage_policy(provider).mayStoreExcerpt:
        stored_text = firewall.excerpt(sanitized_text, limit=2000)

    url_hash = crypto.url_hash(url)
    existing = db.query_one(
        "SELECT id FROM source_document WHERE tenant_id = ? AND url_hash = ? "
        "AND campaign_id IS ?",
        (tenant_id, url_hash, campaign_id),
    )
    payload = {
        "fetch_status": fetch_status,
        "http_status": http_status,
        "mime": mime,
        "bytes": byte_count,
        "content_hash": crypto.content_hash(sanitized_text) if sanitized_text else None,
        "sanitized_text": stored_text,
        "title": title,
        "classification": classification,
        "robots_decision": robots_decision,
        "block_reason": block_reason,
        "fetched_at": clock.now_iso() if fetch_status == FETCH_OK else None,
        "stale_at": clock.days_from_now(stale_days),
    }
    if existing is not None:
        db.update("source_document", existing["id"], payload)
        return existing["id"]

    document_id = db.new_id("src")
    payload.update({
        "id": document_id,
        "tenant_id": tenant_id,
        "campaign_id": campaign_id,
        "url": url,
        "url_hash": url_hash,
        "domain": domain,
        "provider": provider,
        "created_at": clock.now_iso(),
    })
    db.insert("source_document", payload)
    return document_id


def get_source_document(document_id):
    return db.query_one("SELECT * FROM source_document WHERE id = ?", (document_id,))


def stale_documents(tenant_id=None, limit=50):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    return db.query(
        "SELECT * FROM source_document WHERE tenant_id = ? AND fetch_status = ? "
        "AND stale_at IS NOT NULL AND stale_at <= ? ORDER BY stale_at LIMIT ?",
        (tenant_id, FETCH_OK, clock.now_iso(), limit),
    )


def purge_expired(tenant_id=None):
    """Retention enforcement: drop provider data past its retention window."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    removed = 0
    for row in db.query("SELECT * FROM source_document WHERE tenant_id = ?", (tenant_id,)):
        days = firewall.retention_days(row["provider"])
        if not days or not row["created_at"]:
            continue
        age = clock.days_since(row["created_at"])
        if age is not None and age > days:
            db.execute("DELETE FROM source_document WHERE id = ?", (row["id"],))
            removed += 1
    return removed


def delete_provider_data(provider, tenant_id=None):
    """Delete-on-disconnect for a provider whose policy requires it."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    rbac.require("data.delete")
    documents = db.query(
        "SELECT id FROM source_document WHERE tenant_id = ? AND provider = ?",
        (tenant_id, provider),
    )
    ids = [row["id"] for row in documents]
    for document_id in ids:
        db.execute("UPDATE evidence_packet SET source_document_id = NULL "
                   "WHERE source_document_id = ?", (document_id,))
        db.execute("DELETE FROM source_document WHERE id = ?", (document_id,))
    db.execute(
        "DELETE FROM evidence_packet WHERE tenant_id = ? AND source_type = ?",
        (tenant_id, provider.upper()),
    )
    return len(ids)
