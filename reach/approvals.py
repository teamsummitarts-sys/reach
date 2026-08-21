"""Approval integrity and the send executor.

An approval binds to a payload hash, not to a draft id. If the recipient,
subject, body, link, attachment, cost or sending identity changes afterwards,
the hash changes and the approval is dead. The executor delivers the approved
payload or it delivers nothing — it never re-renders, re-addresses or "fixes"
a message on the way out.
"""

import json

from . import (audit, campaigns, clock, compliance, contacts, db, drafts, entities,
               policy, rbac, sender)
from .errors import ApprovalInvalid, SendBlocked, ValidationError
from .providers import email as email_provider


def approval_view(target_id):
    """Exactly what the approver must see before saying yes."""
    target = campaigns.get_target(target_id)
    if target is None:
        return None
    draft = drafts.for_target(target_id)
    if draft is None:
        return None

    method = _method_for(target)
    outlet = entities.get_outlet(target["outlet_id"])
    route = entities.best_route(target["outlet_id"])
    decision_row = compliance.latest_decision(target_id)
    decision = compliance.decision_record(decision_row)
    sendability = contacts.sendability(method["id"]) if method else None
    identity = sender.identity(target["tenant_id"])
    evidence_rows = contacts.method_evidence(method["id"]) if method else []
    from . import scoring

    risk = scoring.latest_risk(target_id)
    score = scoring.latest_score(target_id)

    return {
        "target": dict(target),
        "draft": dict(draft),
        "facts": drafts.facts(draft["id"]),
        "recipient_preview": method["value_preview"] if method else None,
        "recipient_category": method["category"] if method else None,
        "source_of_contact": evidence_rows[0]["source_url"] if evidence_rows else None,
        "independent_verification": [
            {"url": row["source_url"], "domain": row["source_domain"],
             "retrieved_at": row["retrieved_at"], "excerpt": row["excerpt"]}
            for row in evidence_rows
        ],
        "subject": draft["subject"],
        "body": draft["body"],
        "links": json.loads(draft["links_json"] or "[]"),
        "attachments": json.loads(draft["attachments_json"] or "[]"),
        "campaign_id": target["campaign_id"],
        "sending_identity": identity,
        "compliance": decision,
        "compliance_decision": decision_row["decision"] if decision_row else None,
        "risk": dict(risk) if risk else None,
        "reach_score": score["score"] if score else None,
        "cost": route["cost_amount"] if route and route["cost_amount"] else 0.0,
        "cost_model": route["cost_model"] if route else None,
        "sendability": sendability,
        "payload_hash": draft["payload_hash"],
        "follow_up_rule": "One follow-up after 14 days, never after a decline or opt-out.",
        "outlet": dict(outlet) if outlet else None,
    }


def _method_for(target):
    if not target["contact_id"]:
        return None
    rows = contacts.methods_for_contact(target["contact_id"])
    for row in rows:
        if row["category"] in contacts.SENDABLE_CATEGORIES:
            return row
    return rows[0] if rows else None


def approve(target_id, cost=0.0, tenant_id=None):
    """Record an approval bound to the draft's current payload hash."""
    principal = rbac.require("outreach.approve")
    tenant_id = tenant_id or principal.tenant_id

    view = approval_view(target_id)
    if view is None:
        raise ValidationError("Nothing to approve for this target")

    if view["compliance_decision"] in (compliance.BLOCK, compliance.DRAFT_ONLY,
                                       compliance.LEGAL_REVIEW):
        raise SendBlocked(
            f"Compliance decision is {view['compliance_decision']} — this target "
            "cannot be approved for direct email"
        )
    if view["sendability"] and not view["sendability"]["sendable"]:
        raise SendBlocked("; ".join(view["sendability"]["blockers"]))
    if cost and not principal.can("spend.approve"):
        raise SendBlocked("Approving a paid action requires the spend.approve permission")

    draft = view["draft"]
    approval_id = db.new_id("appr")
    db.insert("approval", {
        "id": approval_id,
        "tenant_id": tenant_id,
        "draft_id": draft["id"],
        "target_id": target_id,
        "approved_by": principal.id,
        "approved_at": clock.now_iso(),
        "payload_hash": draft["payload_hash"],
        "cost": float(cost or 0),
        "invalidated_at": None,
        "invalidation_reason": None,
    })
    db.update("outreach_draft", draft["id"], {"status": drafts.APPROVED,
                                              "updated_at": clock.now_iso()})
    campaigns.set_target_status(target_id, campaigns.APPROVED,
                                reason=f"Approved by {principal.email}")
    audit.record("outreach.approved", entity_type="approval", entity_id=approval_id,
                 payload={"target_id": target_id, "payload_hash": draft["payload_hash"],
                          "cost": float(cost or 0)},
                 actor_kind=audit.ACTOR_USER, actor_id=principal.id)
    return approval_id


def latest_approval(target_id):
    return db.query_one(
        "SELECT * FROM approval WHERE target_id = ? AND invalidated_at IS NULL "
        "ORDER BY approved_at DESC LIMIT 1",
        (target_id,),
    )


def invalidate_for_draft(draft_id, reason):
    rows = db.query(
        "SELECT id FROM approval WHERE draft_id = ? AND invalidated_at IS NULL",
        (draft_id,),
    )
    for row in rows:
        db.update("approval", row["id"], {
            "invalidated_at": clock.now_iso(),
            "invalidation_reason": reason,
        })
        audit.record("approval.invalidated", entity_type="approval", entity_id=row["id"],
                     payload={"reason": reason})
    return len(rows)


def approval_is_valid(target_id):
    """An approval is valid only while it still matches the live payload."""
    approval = latest_approval(target_id)
    if approval is None:
        # Distinguish "never approved" from "approved, then revoked" — the
        # reviewer needs to know which one they are looking at.
        revoked = db.query_one(
            "SELECT * FROM approval WHERE target_id = ? AND invalidated_at IS NOT NULL "
            "ORDER BY invalidated_at DESC LIMIT 1",
            (target_id,),
        )
        if revoked is not None:
            return False, (revoked["invalidation_reason"]
                           or "Approval was invalidated because the draft changed after approval")
        return False, "No approval on record"
    draft = drafts.get(approval["draft_id"])
    if draft is None:
        return False, "Approved draft no longer exists"
    if draft["payload_hash"] != approval["payload_hash"]:
        return False, ("Approved payload no longer matches the draft — a material "
                       "field changed after approval")
    return True, None


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

def send_approved(target_id, tenant_id=None):
    """Deliver exactly the approved payload. Every gate is re-checked here.

    A gate that passed at approval time is checked again at send time, because
    suppression, kill switches and sender health can all change in between.
    """
    principal = rbac.require("outreach.send")
    tenant_id = tenant_id or principal.tenant_id

    valid, reason = approval_is_valid(target_id)
    if not valid:
        raise ApprovalInvalid(reason)

    approval = latest_approval(target_id)
    draft = drafts.get(approval["draft_id"])
    target = campaigns.get_target(target_id)
    campaign = campaigns.get(target["campaign_id"])

    if policy.send_stop_engaged():
        raise SendBlocked("Emergency send stop is engaged")
    if campaign["mode"] == campaigns.SCOUT:
        raise SendBlocked("Scout mode never sends")

    health = sender.health_summary(tenant_id)
    if not health["ready"]:
        raise SendBlocked(
            "Sender health has not passed: " + ", ".join(health["failing"])
        )

    throttle = sender.throttle_state(target["campaign_id"], tenant_id)
    if not throttle["ok"]:
        raise SendBlocked("; ".join(throttle["blockers"]))

    method = _method_for(target)
    if method is None:
        raise SendBlocked("No contact method on this target")
    if method["value_hash"] != draft["recipient_hash"]:
        raise ApprovalInvalid("Recipient changed after approval")

    sendability = contacts.sendability(method["id"], tenant_id)
    if not sendability["sendable"]:
        raise SendBlocked("; ".join(sendability["blockers"]))

    if target["status"] in campaigns.TERMINAL_NO_CONTACT:
        raise SendBlocked(f"Target status {target['status']} forbids further contact")

    cooldown, days_left = sender.recipient_in_cooldown(method["id"], tenant_id)
    if cooldown:
        raise SendBlocked(f"Recipient is in cooldown for another {days_left} day(s)")

    address = contacts.reveal(method["id"])
    if not address:
        raise SendBlocked(
            "Contact value could not be decrypted with the configured key. "
            "Set REACH_ENCRYPTION_KEY so stored contacts survive a restart."
        )

    identity = sender.identity(tenant_id)
    # Re-derive the hash from what is about to be sent. If it differs from the
    # approval, nothing goes out.
    live_hash = drafts.payload_hash(
        method["value_hash"], draft["subject"], draft["body"],
        json.loads(draft["links_json"] or "[]"), [], approval["cost"],
        identity["from_email"],
    )
    if live_hash != approval["payload_hash"]:
        raise ApprovalInvalid("Live payload does not match the approved payload hash")

    submission_id = db.new_id("sub")
    db.insert("submission", {
        "id": submission_id,
        "tenant_id": tenant_id,
        "target_id": target_id,
        "route_id": target["route_id"],
        "approval_id": approval["id"],
        "method": "EMAIL",
        "provider": "email",
        "provider_message_id": None,
        "payload_hash": approval["payload_hash"],
        "status": "QUEUED",
        "cost": approval["cost"],
        "sent_at": None,
        "recorded_by": principal.id,
        "error": None,
        "created_at": clock.now_iso(),
    })
    campaigns.set_target_status(target_id, campaigns.QUEUED)

    result = email_provider.send(
        to_address=address,
        subject=draft["subject"],
        body_text=draft["body"],
        from_email=identity["from_email"],
        from_name=identity["from_name"],
        headers={
            "List-Unsubscribe": f"<mailto:{identity['from_email']}?subject=unsubscribe>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
        campaign_id=target["campaign_id"],
    )

    if not result.accepted:
        db.update("submission", submission_id, {
            "status": "FAILED", "error": result.error,
        })
        campaigns.set_target_status(target_id, campaigns.READY,
                                    reason=f"Send failed: {result.error}")
        audit.record("outreach.send_failed", entity_type="submission",
                     entity_id=submission_id, payload={"error": result.error},
                     actor_kind=audit.ACTOR_USER, actor_id=principal.id)
        raise SendBlocked(f"Provider rejected the message: {result.error}")

    now = clock.now_iso()
    db.update("submission", submission_id, {
        "status": "SENT",
        "provider_message_id": result.provider_message_id,
        "sent_at": now,
    })
    db.update("outreach_draft", draft["id"], {"status": drafts.SENT, "updated_at": now})
    campaigns.set_target_status(target_id, campaigns.SENT)
    campaigns.spend_budget(target["campaign_id"], emails=1, spend=approval["cost"])
    _touch_relationship(target, now)
    _schedule_follow_up(target_id, tenant_id)

    audit.record("outreach.sent", entity_type="submission", entity_id=submission_id,
                 payload={"target_id": target_id, "provider_message_id":
                          result.provider_message_id, "mode": result.mode},
                 actor_kind=audit.ACTOR_USER, actor_id=principal.id)
    return submission_id


def _touch_relationship(target, now):
    from . import relationships

    relationships.touch(target["contact_id"], target["outlet_id"], last_contact_at=now)


def _schedule_follow_up(target_id, tenant_id, days=14):
    existing = db.query_one(
        "SELECT id FROM follow_up WHERE target_id = ? AND status = 'PENDING'", (target_id,)
    )
    if existing:
        return existing["id"]
    follow_up_id = db.new_id("fu")
    db.insert("follow_up", {
        "id": follow_up_id,
        "tenant_id": tenant_id,
        "target_id": target_id,
        "sequence": 1,
        "due_at": clock.days_from_now(days),
        "status": "PENDING",
        "sent_at": None,
        "created_at": clock.now_iso(),
    })
    return follow_up_id


def cancel_follow_ups(target_id, reason):
    rows = db.query(
        "SELECT id FROM follow_up WHERE target_id = ? AND status = 'PENDING'", (target_id,)
    )
    for row in rows:
        db.update("follow_up", row["id"], {"status": "CANCELLED"})
    if rows:
        audit.record("follow_up.cancelled", entity_type="campaign_target",
                     entity_id=target_id, payload={"reason": reason, "count": len(rows)})
    return len(rows)


def approval_queue(campaign_id, limit=100):
    """Targets that have a draft and are waiting on a human."""
    return db.query(
        "SELECT t.id AS target_id, t.status, o.name AS outlet_name, o.kind, "
        "d.id AS draft_id, d.subject, d.recipient_preview, d.payload_hash, "
        "d.translation_flagged, "
        "(SELECT score FROM opportunity_score s WHERE s.target_id = t.id "
        " ORDER BY created_at DESC LIMIT 1) AS reach_score, "
        "(SELECT band FROM risk_assessment r WHERE r.target_id = t.id "
        " ORDER BY created_at DESC LIMIT 1) AS risk_band, "
        "(SELECT decision FROM compliance_decision c WHERE c.target_id = t.id "
        " ORDER BY decided_at DESC LIMIT 1) AS compliance_decision, "
        "(SELECT COUNT(*) FROM approval a WHERE a.target_id = t.id "
        " AND a.invalidated_at IS NULL) AS approvals "
        "FROM campaign_target t "
        "JOIN outlet o ON o.id = t.outlet_id "
        "JOIN outreach_draft d ON d.target_id = t.id "
        "WHERE t.campaign_id = ? AND t.status IN (?, ?, ?, ?) "
        "ORDER BY reach_score DESC LIMIT ?",
        (campaign_id, campaigns.DRAFTED, campaigns.NEEDS_APPROVAL,
         campaigns.APPROVED, campaigns.READY, limit),
    )
