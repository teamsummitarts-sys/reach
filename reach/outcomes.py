"""Responses and placements.

Two rules shape this module:

* acceptance is not placement — a positive reply records a response, and only
  evidence records a placement;
* a placement row cannot exist without an evidence packet, enforced by the
  schema's NOT NULL foreign key and again here.
"""

from . import (approvals, audit, campaigns, clock, compliance, contacts, db, evidence,
               rbac, relationships, sender)
from .errors import ValidationError

# --- response kinds --------------------------------------------------------

REPLY = "REPLY"
ACCEPT = "ACCEPT"
DECLINE = "DECLINE"
OPT_OUT = "OPT_OUT"
BOUNCE = "BOUNCE"
AUTO_REPLY = "AUTO_REPLY"
RESPONSE_KINDS = [REPLY, ACCEPT, DECLINE, OPT_OUT, BOUNCE, AUTO_REPLY]

POSITIVE = "POSITIVE"
NEUTRAL = "NEUTRAL"
NEGATIVE = "NEGATIVE"


def record_response(target_id, kind, body_excerpt=None, sentiment=None, received_at=None,
                    tenant_id=None):
    """Record what actually came back. No automated reply is ever generated."""
    principal = rbac.require("outcome.record")
    tenant_id = tenant_id or principal.tenant_id
    if kind not in RESPONSE_KINDS:
        raise ValidationError(f"Unknown response kind: {kind}")

    target = campaigns.get_target(target_id)
    if target is None:
        raise ValidationError("Unknown target")

    submission = db.query_one(
        "SELECT * FROM submission WHERE target_id = ? ORDER BY created_at DESC LIMIT 1",
        (target_id,),
    )

    if kind == REPLY and body_excerpt and sender.looks_like_opt_out(body_excerpt):
        kind = OPT_OUT

    response_id = db.new_id("resp")
    db.insert("response", {
        "id": response_id,
        "tenant_id": tenant_id,
        "target_id": target_id,
        "submission_id": submission["id"] if submission else None,
        "kind": kind,
        "sentiment": sentiment or _default_sentiment(kind),
        "body_excerpt": (body_excerpt or "")[:600] or None,
        "received_at": received_at or clock.now_iso(),
        "recorded_by": principal.id,
        "created_at": clock.now_iso(),
    })

    _apply_response(target, kind, response_id, tenant_id)
    audit.record("response.recorded", entity_type="response", entity_id=response_id,
                 payload={"target_id": target_id, "kind": kind},
                 actor_kind=audit.ACTOR_USER, actor_id=principal.id)
    return response_id


def _default_sentiment(kind):
    return {ACCEPT: POSITIVE, DECLINE: NEGATIVE, OPT_OUT: NEGATIVE,
            BOUNCE: NEGATIVE}.get(kind, NEUTRAL)


def _apply_response(target, kind, response_id, tenant_id):
    target_id = target["id"]

    if kind == OPT_OUT:
        method = approvals._method_for(target)
        if method is not None:
            contacts.suppress(contacts.reveal(method["id"]) or method["value_hash"],
                              "Opt-out requested by recipient", scope=contacts.GLOBAL,
                              source="response")
        campaigns.set_target_status(target_id, campaigns.OPTED_OUT,
                                    reason="Recipient opted out")
        approvals.cancel_follow_ups(target_id, "Recipient opted out")
        relationships.block_recontact(target["contact_id"], target["outlet_id"])
        return

    if kind == BOUNCE:
        method = approvals._method_for(target)
        if method is not None:
            contacts.suppress(contacts.reveal(method["id"]) or method["value_hash"],
                              "Permanent bounce", scope=contacts.GLOBAL, source="response")
        db.execute("UPDATE submission SET status = 'BOUNCED' WHERE target_id = ?",
                   (target_id,))
        campaigns.set_target_status(target_id, campaigns.BOUNCED,
                                    reason="Permanent bounce",
                                    rejection_reason="Invalid contact")
        approvals.cancel_follow_ups(target_id, "Address bounced")
        return

    if kind == DECLINE:
        campaigns.set_target_status(target_id, campaigns.DECLINED,
                                    reason="Recipient declined",
                                    rejection_reason="Not a genre fit")
        approvals.cancel_follow_ups(target_id, "Recipient declined")
        relationships.record_outcome(target["contact_id"], target["outlet_id"],
                                     declined=True)
        relationships.block_recontact(target["contact_id"], target["outlet_id"], days=365)
        return

    if kind == ACCEPT:
        # Accepted, not placed. A placement still needs evidence.
        campaigns.set_target_status(
            target_id, campaigns.ACCEPTED,
            reason="Accepted by the outlet — a placement still requires evidence",
        )
        approvals.cancel_follow_ups(target_id, "Accepted")
        relationships.record_outcome(target["contact_id"], target["outlet_id"],
                                     accepted=True)
        return

    campaigns.set_target_status(target_id, campaigns.RESPONDED, reason=f"{kind} received")
    approvals.cancel_follow_ups(target_id, "Recipient replied")


def responses(campaign_id=None, tenant_id=None, limit=200):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    sql = ("SELECT r.*, o.name AS outlet_name, t.campaign_id "
           "FROM response r JOIN campaign_target t ON t.id = r.target_id "
           "JOIN outlet o ON o.id = t.outlet_id WHERE r.tenant_id = ?")
    params = [tenant_id]
    if campaign_id:
        sql += " AND t.campaign_id = ?"
        params.append(campaign_id)
    sql += " ORDER BY r.received_at DESC LIMIT ?"
    params.append(limit)
    return db.query(sql, tuple(params))


def follow_ups_due(tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    return db.query(
        "SELECT f.*, o.name AS outlet_name, t.campaign_id FROM follow_up f "
        "JOIN campaign_target t ON t.id = f.target_id "
        "JOIN outlet o ON o.id = t.outlet_id "
        "WHERE f.tenant_id = ? AND f.status = 'PENDING' AND f.due_at <= ? "
        "AND t.status NOT IN ('OPTED_OUT','DECLINED','BOUNCED','BLOCKED','PLACED') "
        "ORDER BY f.due_at",
        (tenant_id, clock.now_iso()),
    )


# --------------------------------------------------------------------------
# placements
# --------------------------------------------------------------------------

EVIDENCE_API = "PROVIDER_API"
EVIDENCE_URL = "VERIFIED_URL"
EVIDENCE_ANALYTICS = "ARTIST_ANALYTICS"
EVIDENCE_CURATOR = "CURATOR_CONFIRMATION"
EVIDENCE_USER = "USER_PROVIDED_RECORD"
EVIDENCE_MONITOR = "THIRD_PARTY_MONITORING"
EVIDENCE_SOURCES = [EVIDENCE_API, EVIDENCE_URL, EVIDENCE_ANALYTICS, EVIDENCE_CURATOR,
                    EVIDENCE_USER, EVIDENCE_MONITOR]


def record_placement(target_id, evidence_source, url=None, program_name=None,
                     added_date=None, position=None, excerpt=None, confidence=0.8,
                     cost=0.0, notes=None, tenant_id=None):
    """Create a placement. Requires an evidence source — there is no path
    that produces a placement row without one."""
    principal = rbac.require("outcome.record")
    tenant_id = tenant_id or principal.tenant_id

    if evidence_source not in EVIDENCE_SOURCES:
        raise ValidationError(
            f"A placement needs a recognised evidence source, got {evidence_source!r}"
        )
    if not url and evidence_source in (EVIDENCE_URL, EVIDENCE_API):
        raise ValidationError(f"{evidence_source} evidence requires a placement URL")

    target = campaigns.get_target(target_id)
    if target is None:
        raise ValidationError("Unknown target")
    campaign = campaigns.get(target["campaign_id"])

    evidence_id = evidence.record(
        entity_type="placement", entity_id="pending", supports_field="placement",
        value={"url": url, "program": program_name, "position": position},
        source_url=url or (target["outlet_url"] or ""),
        source_domain=target["outlet_domain"] or "",
        source_type=evidence_source,
        excerpt=excerpt or f"Placement recorded from {evidence_source}",
        confidence=confidence, extractor_version="placement/1.0.0",
        verified_at=clock.now_iso(), tenant_id=tenant_id,
    )

    placement_id = db.new_id("plc")
    db.insert("placement", {
        "id": placement_id,
        "tenant_id": tenant_id,
        "campaign_id": target["campaign_id"],
        "target_id": target_id,
        "outlet_id": target["outlet_id"],
        "recording_id": campaign["recording_id"],
        "url": url,
        "program_name": program_name,
        "added_date": added_date or clock.now_iso()[:10],
        "verified_at": clock.now_iso(),
        "removed_date": None,
        "position": position,
        "evidence_id": evidence_id,
        "evidence_source": evidence_source,
        "confidence": confidence,
        "cost": float(cost or 0),
        "notes": notes,
        "created_at": clock.now_iso(),
    })
    db.update("evidence_packet", evidence_id, {"entity_id": placement_id})

    campaigns.set_target_status(target_id, campaigns.PLACED,
                                reason=f"Placement verified via {evidence_source}")
    relationships.record_outcome(target["contact_id"], target["outlet_id"], placed=True)
    audit.record("placement.recorded", entity_type="placement", entity_id=placement_id,
                 payload={"target_id": target_id, "evidence_source": evidence_source},
                 actor_kind=audit.ACTOR_USER, actor_id=principal.id)
    return placement_id


def mark_removed(placement_id, removed_date=None):
    rbac.require("outcome.record")
    db.update("placement", placement_id, {
        "removed_date": removed_date or clock.now_iso()[:10],
    })
    audit.record("placement.removed", entity_type="placement", entity_id=placement_id,
                 actor_kind=audit.ACTOR_USER)


def placements(campaign_id=None, tenant_id=None, limit=200):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    sql = ("SELECT p.*, o.name AS outlet_name, o.kind AS outlet_kind, r.title AS recording_title "
           "FROM placement p JOIN outlet o ON o.id = p.outlet_id "
           "JOIN recording r ON r.id = p.recording_id WHERE p.tenant_id = ?")
    params = [tenant_id]
    if campaign_id:
        sql += " AND p.campaign_id = ?"
        params.append(campaign_id)
    sql += " ORDER BY p.created_at DESC LIMIT ?"
    params.append(limit)
    return db.query(sql, tuple(params))


def placement_evidence(placement_id):
    row = db.query_one("SELECT evidence_id FROM placement WHERE id = ?", (placement_id,))
    if row is None:
        return None
    return evidence.get(row["evidence_id"])


def active_placement_count(campaign_id=None, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    sql = "SELECT COUNT(*) AS n FROM placement WHERE tenant_id = ? AND removed_date IS NULL"
    params = [tenant_id]
    if campaign_id:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    row = db.query_one(sql, tuple(params))
    return row["n"] if row else 0


# --------------------------------------------------------------------------
# bounce / complaint webhook processing
# --------------------------------------------------------------------------

def process_provider_event(kind, address, target_id=None, tenant_id=None):
    """Handle a verified provider webhook event."""
    if kind in ("bounce", "hard_bounce", "permanent_failure"):
        compliance.record_bounce(address, permanent=True, source="provider_webhook")
        if target_id:
            record_response(target_id, BOUNCE, body_excerpt="Permanent bounce",
                            tenant_id=tenant_id)
        return "SUPPRESSED"
    if kind in ("complaint", "spam_complaint"):
        contacts.suppress(address, "Spam complaint", scope=contacts.GLOBAL,
                          source="provider_webhook")
        return "SUPPRESSED"
    if kind in ("unsubscribe", "opt_out"):
        compliance.record_opt_out(address, source="provider_webhook")
        if target_id:
            record_response(target_id, OPT_OUT, tenant_id=tenant_id)
        return "SUPPRESSED"
    return "IGNORED"
