"""Campaigns and the target CRM.

Statuses are the campaign's memory: every target moves through them explicitly,
and every metric on the Overview screen is a count of these rows rather than a
decorative number.
"""

import json

from . import audit, catalog, clock, config, db, profile, rbac
from .errors import ValidationError

SCOUT = "SCOUT"
COPILOT = "COPILOT"
AUTOPILOT = "AUTOPILOT"
MODES = [SCOUT, COPILOT, AUTOPILOT]

DRAFT = "DRAFT"
ACTIVE = "ACTIVE"
PAUSED = "PAUSED"
COMPLETED = "COMPLETED"
CANCELLED = "CANCELLED"
CAMPAIGN_STATUSES = [DRAFT, ACTIVE, PAUSED, COMPLETED, CANCELLED]

# --- target statuses -------------------------------------------------------

DISCOVERED = "DISCOVERED"
ENRICHING = "ENRICHING"
UNVERIFIED = "UNVERIFIED"
QUALIFIED = "QUALIFIED"
DISQUALIFIED = "DISQUALIFIED"
DUPLICATE = "DUPLICATE"
STALE = "STALE"
READY = "READY"
DRAFTED = "DRAFTED"
NEEDS_APPROVAL = "NEEDS_APPROVAL"
APPROVED = "APPROVED"
QUEUED = "QUEUED"
SENT = "SENT"
SUBMITTED = "SUBMITTED"
DELIVERED = "DELIVERED"
BOUNCED = "BOUNCED"
RESPONDED = "RESPONDED"
FOLLOW_UP_DUE = "FOLLOW_UP_DUE"
ACCEPTED = "ACCEPTED"
PLACED = "PLACED"
DECLINED = "DECLINED"
EXPIRED = "EXPIRED"
OPTED_OUT = "OPTED_OUT"
BLOCKED = "BLOCKED"

TARGET_STATUSES = [
    DISCOVERED, ENRICHING, UNVERIFIED, QUALIFIED, DISQUALIFIED, DUPLICATE, STALE,
    READY, DRAFTED, NEEDS_APPROVAL, APPROVED, QUEUED, SENT, SUBMITTED, DELIVERED,
    BOUNCED, RESPONDED, FOLLOW_UP_DUE, ACCEPTED, PLACED, DECLINED, EXPIRED,
    OPTED_OUT, BLOCKED,
]

# Statuses that must never receive another message.
TERMINAL_NO_CONTACT = {OPTED_OUT, BLOCKED, DECLINED, BOUNCED}

# Stage occupancy, not a cumulative funnel: a target sits in exactly one stage.
# The first stage is named "In discovery" so it cannot be misread as the total
# discovered count shown on the Overview header.
PIPELINE_STAGES = [
    ("In discovery", [DISCOVERED, ENRICHING, UNVERIFIED]),
    ("Qualified", [QUALIFIED]),
    ("Ready", [READY, DRAFTED, NEEDS_APPROVAL, APPROVED, QUEUED]),
    ("Submitted", [SENT, SUBMITTED, DELIVERED]),
    ("Responded", [RESPONDED, FOLLOW_UP_DUE, ACCEPTED]),
    ("Placed", [PLACED]),
    ("Closed", [DISQUALIFIED, DUPLICATE, STALE, BOUNCED, DECLINED, EXPIRED,
                OPTED_OUT, BLOCKED]),
]

REJECTION_REASONS = [
    "Not a genre fit", "Submissions closed", "Release timing", "Production quality",
    "Missing assets", "No response", "Paid service only", "Suspicious route",
    "Duplicate", "Invalid contact", "Unknown",
]

CHANNELS = ["PLAYLIST", "RADIO", "BLOG", "PUBLICATION", "DJ", "DJ_POOL", "PODCAST",
            "CREATOR", "CURATOR"]


# --------------------------------------------------------------------------
# campaigns
# --------------------------------------------------------------------------

def create(recording_id, name=None, mode=SCOUT, territories=None, channels=None,
           languages=None, start_date=None, end_date=None, search_budget=None,
           domain_budget=None, daily_send_limit=None, priorities=None):
    """A campaign cannot exist without a rights attestation and a complete-enough
    track profile. Both are checked here rather than at send time."""
    principal = rbac.require("campaign.create")

    if mode not in MODES:
        raise ValidationError(f"Unknown campaign mode: {mode}")
    if mode == AUTOPILOT and not config.feature_enabled("reach.autopilot"):
        raise ValidationError(
            "Autopilot is disabled in Phase One. Security, compliance, sender health "
            "and approval-integrity reviews must pass before it can be enabled."
        )

    recording = catalog.get_recording(recording_id)
    if recording is None:
        raise ValidationError("Unknown recording")

    attestation = catalog.active_attestation(recording_id)
    if attestation is None:
        raise ValidationError(
            "A rights attestation is required before a campaign can launch."
        )

    profile_id = profile.get_or_create(recording_id)
    completeness = profile.completeness(profile_id)
    if not completeness["ready"]:
        raise ValidationError(
            "Track intelligence profile is incomplete. Missing required fields: "
            + ", ".join(completeness["missing_required"])
        )

    campaign_id = db.new_id("camp")
    now = clock.now_iso()
    db.insert("campaign", {
        "id": campaign_id,
        "tenant_id": principal.tenant_id,
        "recording_id": recording_id,
        "rights_attestation_id": attestation["id"],
        "name": name or f"{recording['title']} — REACH campaign",
        "mode": mode,
        "status": DRAFT,
        "territories_json": json.dumps(territories or []),
        "channels_json": json.dumps(channels or CHANNELS),
        "languages_json": json.dumps(languages or ["en"]),
        "start_date": start_date,
        "end_date": end_date,
        "search_budget": search_budget or config.DEFAULT_SEARCH_BUDGET,
        "domain_budget": domain_budget or config.DEFAULT_DOMAIN_BUDGET,
        "daily_send_limit": daily_send_limit or config.DEFAULT_DAILY_SEND_LIMIT,
        "priorities_json": json.dumps(priorities or []),
        "created_by": principal.id,
        "created_at": now,
        "updated_at": now,
    })
    db.insert("campaign_budget", {
        "campaign_id": campaign_id,
        "searches_used": 0,
        "pages_fetched": 0,
        "model_tokens": 0,
        "emails_sent": 0,
        "spend": 0.0,
        "domains_json": "{}",
        "updated_at": now,
    })
    audit.record("campaign.created", entity_type="campaign", entity_id=campaign_id,
                 payload={"mode": mode, "recording_id": recording_id},
                 actor_kind=audit.ACTOR_USER, actor_id=principal.id)
    return campaign_id


def get(campaign_id):
    return db.query_one("SELECT * FROM campaign WHERE id = ?", (campaign_id,))


def list_campaigns(tenant_id=None, limit=50):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    return db.query(
        "SELECT c.*, r.title AS recording_title, a.name AS artist_name "
        "FROM campaign c "
        "JOIN recording r ON r.id = c.recording_id "
        "JOIN artist a ON a.id = r.artist_id "
        "WHERE c.tenant_id = ? ORDER BY c.created_at DESC LIMIT ?",
        (tenant_id, limit),
    )


def set_status(campaign_id, status):
    if status not in CAMPAIGN_STATUSES:
        raise ValidationError(f"Unknown campaign status: {status}")
    rbac.require("campaign.create")
    db.update("campaign", campaign_id, {"status": status, "updated_at": clock.now_iso()})
    audit.record("campaign.status", entity_type="campaign", entity_id=campaign_id,
                 payload={"status": status}, actor_kind=audit.ACTOR_USER)


def settings(campaign_id):
    row = get(campaign_id)
    if row is None:
        return None
    return {
        "territories": json.loads(row["territories_json"] or "[]"),
        "channels": json.loads(row["channels_json"] or "[]"),
        "languages": json.loads(row["languages_json"] or "[]"),
        "priorities": json.loads(row["priorities_json"] or "[]"),
        "search_budget": row["search_budget"],
        "domain_budget": row["domain_budget"],
        "daily_send_limit": row["daily_send_limit"],
    }


# --------------------------------------------------------------------------
# targets
# --------------------------------------------------------------------------

def add_target(campaign_id, outlet_id, contact_id=None, route_id=None,
               status=DISCOVERED, dedup_key=None, related_outlets=None,
               reason=None, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    existing = db.query_one(
        "SELECT * FROM campaign_target WHERE campaign_id = ? AND outlet_id = ?",
        (campaign_id, outlet_id),
    )
    now = clock.now_iso()
    if existing is not None:
        payload = {
            "contact_id": contact_id or existing["contact_id"],
            "route_id": route_id or existing["route_id"],
            "dedup_key": dedup_key or existing["dedup_key"],
            "updated_at": now,
        }
        # Never wipe an accumulated consolidation list on a re-resolve.
        if related_outlets:
            payload["related_outlets_json"] = json.dumps(related_outlets)
        db.update("campaign_target", existing["id"], payload)
        return existing["id"]

    target_id = db.new_id("tgt")
    db.insert("campaign_target", {
        "id": target_id,
        "tenant_id": tenant_id,
        "campaign_id": campaign_id,
        "outlet_id": outlet_id,
        "contact_id": contact_id,
        "route_id": route_id,
        "status": status,
        "status_reason": reason,
        "owner_principal_id": None,
        "dedup_key": dedup_key,
        "related_outlets_json": json.dumps(related_outlets or []),
        "tags_json": json.dumps([]),
        "rejection_reason": None,
        "created_at": now,
        "updated_at": now,
    })
    return target_id


def get_target(target_id):
    return db.query_one(
        "SELECT t.*, o.name AS outlet_name, o.url AS outlet_url, o.kind AS outlet_kind, "
        "o.domain AS outlet_domain, o.submissions_open, o.description AS outlet_description, "
        "o.genres_json, o.territory, o.follower_count, o.follower_count_source, "
        "o.last_activity_at "
        "FROM campaign_target t JOIN outlet o ON o.id = t.outlet_id WHERE t.id = ?",
        (target_id,),
    )


def targets(campaign_id, status=None, statuses=None, limit=500, order="score"):
    sql = (
        "SELECT t.*, o.name AS outlet_name, o.url AS outlet_url, o.kind AS outlet_kind, "
        "o.domain AS outlet_domain, o.submissions_open, o.genres_json, o.territory, "
        "o.follower_count, o.last_activity_at, "
        "(SELECT score FROM opportunity_score s WHERE s.target_id = t.id "
        " ORDER BY created_at DESC LIMIT 1) AS reach_score, "
        "(SELECT components_json FROM opportunity_score s WHERE s.target_id = t.id "
        " ORDER BY created_at DESC LIMIT 1) AS score_components_json, "
        "(SELECT score FROM risk_assessment r WHERE r.target_id = t.id "
        " ORDER BY created_at DESC LIMIT 1) AS risk_score, "
        "(SELECT band FROM risk_assessment r WHERE r.target_id = t.id "
        " ORDER BY created_at DESC LIMIT 1) AS risk_band, "
        "(SELECT decision FROM compliance_decision c WHERE c.target_id = t.id "
        " ORDER BY decided_at DESC LIMIT 1) AS compliance_decision "
        "FROM campaign_target t JOIN outlet o ON o.id = t.outlet_id "
        "WHERE t.campaign_id = ?"
    )
    params = [campaign_id]
    if status:
        sql += " AND t.status = ?"
        params.append(status)
    elif statuses:
        sql += " AND t.status IN (" + ",".join("?" for _ in statuses) + ")"
        params.extend(statuses)
    sql += (" ORDER BY reach_score DESC NULLS LAST, t.created_at"
            if order == "score" else " ORDER BY t.created_at DESC")
    sql += " LIMIT ?"
    params.append(limit)
    return db.query(sql, tuple(params))


def set_target_status(target_id, status, reason=None, rejection_reason=None):
    if status not in TARGET_STATUSES:
        raise ValidationError(f"Unknown target status: {status}")
    payload = {"status": status, "updated_at": clock.now_iso()}
    if reason is not None:
        payload["status_reason"] = reason
    if rejection_reason is not None:
        if rejection_reason not in REJECTION_REASONS:
            raise ValidationError(f"Unknown rejection reason: {rejection_reason}")
        payload["rejection_reason"] = rejection_reason
    db.update("campaign_target", target_id, payload)
    audit.record("target.status", entity_type="campaign_target", entity_id=target_id,
                 payload={"status": status, "reason": reason})
    return status


def assign_owner(target_id, principal_id):
    rbac.require("target.edit")
    db.update("campaign_target", target_id, {"owner_principal_id": principal_id,
                                             "updated_at": clock.now_iso()})


def set_tags(target_id, tags):
    rbac.require("target.edit")
    db.update("campaign_target", target_id, {"tags_json": json.dumps(sorted(set(tags))),
                                             "updated_at": clock.now_iso()})


def status_counts(campaign_id):
    rows = db.query(
        "SELECT status, COUNT(*) AS n FROM campaign_target WHERE campaign_id = ? "
        "GROUP BY status",
        (campaign_id,),
    )
    return {row["status"]: row["n"] for row in rows}


def pipeline_counts(campaign_id):
    counts = status_counts(campaign_id)
    result = []
    for label, statuses in PIPELINE_STAGES:
        result.append({
            "label": label,
            "statuses": statuses,
            "count": sum(counts.get(status, 0) for status in statuses),
        })
    return result


def budget(campaign_id):
    row = db.query_one("SELECT * FROM campaign_budget WHERE campaign_id = ?", (campaign_id,))
    if row is None:
        return None
    return {
        "searches_used": row["searches_used"],
        "pages_fetched": row["pages_fetched"],
        "model_tokens": row["model_tokens"],
        "emails_sent": row["emails_sent"],
        "spend": row["spend"],
        "domains": json.loads(row["domains_json"] or "{}"),
        "updated_at": row["updated_at"],
    }


def spend_budget(campaign_id, searches=0, pages=0, tokens=0, emails=0, spend=0.0,
                 domain=None):
    row = db.query_one("SELECT * FROM campaign_budget WHERE campaign_id = ?", (campaign_id,))
    if row is None:
        return None
    domains = json.loads(row["domains_json"] or "{}")
    if domain:
        domains[domain] = domains.get(domain, 0) + 1
    db.execute(
        "UPDATE campaign_budget SET searches_used = searches_used + ?, "
        "pages_fetched = pages_fetched + ?, model_tokens = model_tokens + ?, "
        "emails_sent = emails_sent + ?, spend = spend + ?, domains_json = ?, "
        "updated_at = ? WHERE campaign_id = ?",
        (searches, pages, tokens, emails, spend, json.dumps(domains),
         clock.now_iso(), campaign_id),
    )
    return domains


def domain_budget_available(campaign_id, domain):
    campaign = get(campaign_id)
    if campaign is None:
        return False
    used = (budget(campaign_id) or {}).get("domains", {}).get(domain, 0)
    return used < campaign["domain_budget"]
