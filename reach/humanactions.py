"""The NEEDS YOU queue and the DSP editorial-deadline engine.

Every route REACH cannot legitimately automate becomes a structured task with
the reason automation is unavailable stated plainly, the field answers prepared,
and the destination one click away. Opening the destination never marks the task
submitted — only a person does that.
"""

import json

from . import audit, campaigns, catalog, clock, db, entities, profile, rbac
from .errors import ValidationError
from .providers import spotify as spotify_provider

OPEN = "OPEN"
IN_PROGRESS = "IN_PROGRESS"
SUBMITTED = "SUBMITTED"
SKIPPED = "SKIPPED"
SNOOZED = "SNOOZED"
BROKEN = "BROKEN_ROUTE"
STATUSES = [OPEN, IN_PROGRESS, SUBMITTED, SKIPPED, SNOOZED, BROKEN]

# Why automation is unavailable. Each maps to a real constraint, not a TODO.
REASON_LOGIN = "Login required — automating a login-walled form is a Phase One non-goal"
REASON_CAPTCHA = "CAPTCHA present — REACH never solves CAPTCHAs"
REASON_FORBIDDEN = "The provider's terms forbid automated submission"
REASON_NO_API = "No submission API exists for this destination"
REASON_ACCOUNT = "Requires an authorized account action by the artist or label"
REASON_PAID = "Paid submission — explicit spend approval required"
REASON_LEGAL = "Legal review required before contact"
REASON_JUDGMENT = "Personal judgment required"
REASON_MISSING_ASSET = "A required asset is missing"
REASON_MISSING_ANSWER = "A required artist answer is missing"

EFFORT_QUICK = "Under 5 minutes"
EFFORT_MEDIUM = "5–15 minutes"
EFFORT_LONG = "Over 15 minutes"


def create(campaign_id, provider, title, action, reason, destination_url=None,
           target_id=None, deadline=None, eligibility=None, fields=None, assets=None,
           pitch_text=None, effort=EFFORT_MEDIUM, cost=0.0, currency=None,
           risk_band=None, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    existing = db.query_one(
        "SELECT id FROM human_action_task WHERE campaign_id IS ? AND provider = ? "
        "AND title = ? AND target_id IS ?",
        (campaign_id, provider, title, target_id),
    )
    now = clock.now_iso()
    if existing is not None:
        db.update("human_action_task", existing["id"], {
            "action": action, "reason": reason, "destination_url": destination_url,
            "deadline": deadline, "eligibility_json": json.dumps(eligibility or []),
            "fields_json": json.dumps(fields or []), "assets_json": json.dumps(assets or []),
            "pitch_text": pitch_text, "updated_at": now,
        })
        return existing["id"]

    task_id = db.new_id("task")
    db.insert("human_action_task", {
        "id": task_id,
        "tenant_id": tenant_id,
        "campaign_id": campaign_id,
        "target_id": target_id,
        "provider": provider,
        "title": title,
        "action": action,
        "reason": reason,
        "destination_url": destination_url,
        "deadline": deadline,
        "eligibility_json": json.dumps(eligibility or []),
        "fields_json": json.dumps(fields or []),
        "assets_json": json.dumps(assets or []),
        "pitch_text": pitch_text,
        "effort": effort,
        "cost": float(cost or 0),
        "currency": currency,
        "risk_band": risk_band,
        "status": OPEN,
        "snooze_until": None,
        "created_at": now,
        "updated_at": now,
    })
    audit.record("human_task.created", entity_type="human_action_task", entity_id=task_id,
                 payload={"provider": provider, "reason": reason})
    return task_id


def create_from_target(target_id, route, decision):
    """Turn a qualified-but-unautomatable target into a task."""
    from . import compliance, drafts, scoring

    target = campaigns.get_target(target_id)
    if target is None:
        return None
    outlet = entities.get_outlet(target["outlet_id"])
    risk = scoring.latest_risk(target_id)

    if route is None:
        reason = REASON_NO_API
        destination = outlet["url"] if outlet else None
    elif route["requires_captcha"]:
        reason = REASON_CAPTCHA
        destination = route["destination"]
    elif route["requires_login"]:
        reason = REASON_LOGIN
        destination = route["destination"]
    elif route["method"] == "WEB_FORM":
        reason = REASON_NO_API
        destination = route["destination"]
    elif decision and decision.get("decision") == compliance.LEGAL_REVIEW:
        reason = REASON_LEGAL
        destination = route["destination"]
    elif route["cost_model"] == "PAID_CONSIDERATION":
        reason = REASON_PAID
        destination = route["destination"]
    else:
        reason = REASON_JUDGMENT
        destination = route["destination"]

    pitch = None
    try:
        draft_id = drafts.generate(target_id)
        pitch = drafts.get(draft_id)["body"]
    except Exception:
        # A task is still worth creating without prepared copy.
        pitch = None

    return create(
        target["campaign_id"], provider="open_web_fetch",
        title=f"Submit to {outlet['name'] if outlet else 'outlet'}",
        action="Complete this outlet's own submission process",
        reason=reason, destination_url=destination, target_id=target_id,
        fields=_route_fields(route),
        cost=(route["cost_amount"] if route and route["cost_amount"] else 0.0),
        currency=(route["currency"] if route else None),
        risk_band=risk["band"] if risk else None,
        pitch_text=pitch,
        effort=EFFORT_QUICK if reason == REASON_JUDGMENT else EFFORT_MEDIUM,
    )


def _route_fields(route):
    if route is None:
        return []
    fields = [{"label": "Submission method", "value": route["method"]}]
    if route["instructions"]:
        fields.append({"label": "Published instructions", "value": route["instructions"]})
    if route["cost_model"] and route["cost_model"] != "UNKNOWN":
        fields.append({"label": "Cost model", "value": route["cost_model"]})
    return fields


# --------------------------------------------------------------------------
# queue
# --------------------------------------------------------------------------

def queue(campaign_id=None, tenant_id=None, include_done=False, limit=200):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    sql = "SELECT * FROM human_action_task WHERE tenant_id = ?"
    params = [tenant_id]
    if campaign_id:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    if not include_done:
        sql += " AND status IN (?, ?, ?)"
        params.extend([OPEN, IN_PROGRESS, SNOOZED])
    sql += " ORDER BY CASE WHEN deadline IS NULL THEN 1 ELSE 0 END, deadline, created_at LIMIT ?"
    params.append(limit)
    rows = db.query(sql, tuple(params))
    return [_expand(row) for row in rows]


def _expand(row):
    item = dict(row)
    item["eligibility"] = json.loads(row["eligibility_json"] or "[]")
    item["fields"] = json.loads(row["fields_json"] or "[]")
    item["assets"] = json.loads(row["assets_json"] or "[]")
    item["deadline_days"] = None
    if row["deadline"]:
        days = clock.days_since(row["deadline"])
        item["deadline_days"] = round(-days) if days is not None else None
    item["snoozed"] = bool(row["snooze_until"] and not clock.is_past(row["snooze_until"]))
    return item


# Short queue-group labels for the reasons above. A hundred undifferentiated
# tasks reads as a wall; "45 CAPTCHA walls" reads as a batch you can sit down
# and clear.
REASON_GROUPS = [
    (REASON_ACCOUNT, "Platform tools"),
    (REASON_CAPTCHA, "CAPTCHA walls"),
    (REASON_LOGIN, "Login required"),
    (REASON_NO_API, "Web forms"),
    (REASON_JUDGMENT, "Web forms"),
    (REASON_PAID, "Paid submissions"),
    (REASON_FORBIDDEN, "Automation forbidden"),
    (REASON_LEGAL, "Legal review"),
]


def open_summary(tenant_id=None):
    """Open-task counts grouped by why each one needs a human."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    rows = db.query(
        "SELECT reason, COUNT(*) AS n FROM human_action_task "
        "WHERE tenant_id = ? AND status IN (?, ?) GROUP BY reason",
        (tenant_id, OPEN, IN_PROGRESS),
    )
    counts = {row["reason"]: row["n"] for row in rows}
    labels = dict(REASON_GROUPS)
    grouped = {}
    for reason, count in counts.items():
        label = labels.get(reason, "Other")
        entry = grouped.setdefault(label, {"label": label, "count": 0, "reasons": []})
        entry["count"] += count
        entry["reasons"].append(reason)
    groups = sorted(grouped.values(), key=lambda g: -g["count"])
    return {"total": sum(counts.values()), "groups": groups}


def open_count(campaign_id=None, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    sql = ("SELECT COUNT(*) AS n FROM human_action_task WHERE tenant_id = ? "
           "AND status IN (?, ?)")
    params = [tenant_id, OPEN, IN_PROGRESS]
    if campaign_id:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    row = db.query_one(sql, tuple(params))
    return row["n"] if row else 0


def get(task_id):
    row = db.query_one("SELECT * FROM human_action_task WHERE id = ?", (task_id,))
    return _expand(row) if row else None


def set_status(task_id, status, note=None):
    if status not in STATUSES:
        raise ValidationError(f"Unknown task status: {status}")
    principal = rbac.require("human_task.complete")
    task = get(task_id)
    if task is None:
        raise ValidationError("Unknown task")

    db.update("human_action_task", task_id, {"status": status,
                                             "updated_at": clock.now_iso()})

    # Marking a task submitted is the only thing that records a submission.
    if status == SUBMITTED and task["target_id"]:
        _record_manual_submission(task, principal)
    if status == BROKEN and task["target_id"]:
        target = campaigns.get_target(task["target_id"])
        if target and target["route_id"]:
            entities.report_broken_route(target["route_id"], note)
        campaigns.set_target_status(task["target_id"], campaigns.DISQUALIFIED,
                                    reason="Route reported broken",
                                    rejection_reason="Invalid contact")

    audit.record("human_task.status", entity_type="human_action_task", entity_id=task_id,
                 payload={"status": status, "note": note},
                 actor_kind=audit.ACTOR_USER, actor_id=principal.id)
    return status


def snooze(task_id, days=7):
    rbac.require("human_task.complete")
    db.update("human_action_task", task_id, {
        "status": SNOOZED,
        "snooze_until": clock.days_from_now(days),
        "updated_at": clock.now_iso(),
    })
    return clock.days_from_now(days)


def _record_manual_submission(task, principal):
    submission_id = db.new_id("sub")
    db.insert("submission", {
        "id": submission_id,
        "tenant_id": task["tenant_id"],
        "target_id": task["target_id"],
        "route_id": None,
        "approval_id": None,
        "method": "HUMAN_ACTION",
        "provider": task["provider"],
        "provider_message_id": None,
        "payload_hash": "manual",
        "status": "SUBMITTED",
        "cost": task["cost"],
        "sent_at": clock.now_iso(),
        "recorded_by": principal.id,
        "error": None,
        "created_at": clock.now_iso(),
    })
    campaigns.set_target_status(task["target_id"], campaigns.SUBMITTED,
                                reason=f"Submitted manually via {task['provider']}")
    return submission_id


# --------------------------------------------------------------------------
# DSP editorial deadline engine
# --------------------------------------------------------------------------

def _copy_ready_answers(recording, facts, values):
    """Field answers a person can paste straight into a provider's form."""
    answers = [
        {"label": "Track name", "value": recording["title"]},
        {"label": "Artist", "value": recording["artist_name"]},
        {"label": "ISRC", "value": recording["isrc"] or "UNKNOWN — add before submitting"},
        {"label": "UPC", "value": recording["upc"] or "UNKNOWN — add before submitting"},
        {"label": "Primary genre", "value": values.get("primary_genre") or "UNKNOWN"},
        {"label": "Microgenres", "value": ", ".join(values.get("microgenres") or []) or "UNKNOWN"},
        {"label": "Mood", "value": ", ".join(values.get("mood") or []) or "UNKNOWN"},
        {"label": "Language", "value": values.get("language") or "UNKNOWN"},
        {"label": "Comparable artists",
         "value": ", ".join(values.get("comparable_artists") or []) or "UNKNOWN"},
        {"label": "Instrumentation",
         "value": ", ".join(values.get("instrumentation") or []) or "UNKNOWN"},
        {"label": "Culture or scene", "value": values.get("cultural_scene") or "UNKNOWN"},
        {"label": "Song description", "value": values.get("release_narrative") or "UNKNOWN"},
        {"label": "Marketing plan", "value": values.get("marketing_angle") or "UNKNOWN"},
        {"label": "Territory focus",
         "value": ", ".join(values.get("geographic_affinity") or []) or "UNKNOWN"},
    ]
    if facts is not None:
        answers.append({"label": "Writers", "value": ", ".join(facts.writers) or "UNKNOWN"})
        answers.append({"label": "Producers", "value": ", ".join(facts.producers) or "UNKNOWN"})
        answers.append({"label": "Publisher", "value": facts.publisher or "UNKNOWN"})
    return answers


def build_dsp_tasks(campaign_id):
    """Create the provider tasks whose eligibility this campaign actually meets."""
    campaign = campaigns.get(campaign_id)
    if campaign is None:
        return []
    recording = catalog.get_recording(campaign["recording_id"])
    facts = catalog.track(recording)
    profile_id = profile.get_or_create(campaign["recording_id"])
    values = profile.values(profile_id)
    answers = _copy_ready_answers(recording, facts, values)
    delivered = bool(facts and facts.distributed)

    created = []

    # --- Spotify -----------------------------------------------------------
    requirements = spotify_provider.editorial_pitch_requirements()
    created.append(create(
        campaign_id, "spotify", "Spotify for Artists editorial pitch",
        action="Open Spotify for Artists and submit the editorial pitch for this release",
        reason=requirements["reason"],
        destination_url=requirements["destination_url"],
        eligibility=[
            {"label": "Release delivered to Spotify", "ok": delivered},
            {"label": "Track is unreleased at pitch time", "ok": None,
             "detail": "REACH cannot verify this — confirm before pitching"},
            {"label": "One song per pitch", "ok": True},
            {"label": "Pitch at least 7 days before release", "ok": None,
             "detail": "Depends on the release date you set"},
        ],
        fields=answers,
        assets=["Cover art", "Artist profile image"],
        effort=EFFORT_MEDIUM,
    ))

    # --- Amazon Music ------------------------------------------------------
    created.append(create(
        campaign_id, "amazon_music", "Amazon Music for Artists new-release pitch",
        action="Open Amazon Music for Artists and complete the new-release pitch",
        reason=REASON_ACCOUNT,
        destination_url="https://artists.amazonmusic.com",
        eligibility=[
            {"label": "Release delivered to Amazon Music", "ok": delivered},
            {"label": "Track has never been previously released", "ok": None},
            {"label": "One track per release", "ok": True},
            {"label": "Team permissions on the artist account", "ok": None},
        ],
        fields=answers + [
            {"label": "Collaborators",
             "value": ", ".join((facts.writers if facts else []) + (facts.producers if facts else []))
                      or "UNKNOWN"},
            {"label": "Prior placements", "value": "UNKNOWN — do not claim placements "
                                                   "REACH has not verified"},
            {"label": "Sync or video plans", "value": values.get("visual_identity") or "UNKNOWN"},
        ],
        effort=EFFORT_MEDIUM,
    ))

    # --- Pandora -----------------------------------------------------------
    created.append(create(
        campaign_id, "pandora", "Pandora AMP submission",
        action="Submit this track through Pandora AMP",
        reason=REASON_ACCOUNT,
        destination_url="https://amp.pandora.com",
        eligibility=[
            {"label": "Delivered through an approved distributor", "ok": delivered},
            {"label": "Pandora selected in distribution", "ok": None},
            {"label": "U.S. rights granted", "ok": None},
            {"label": "Artist account claimed", "ok": None},
            {"label": "Required assets and metadata present",
             "ok": bool(recording["isrc"] and recording["upc"])},
        ],
        fields=answers,
        effort=EFFORT_MEDIUM,
    ))

    # --- Audiomack ---------------------------------------------------------
    created.append(create(
        campaign_id, "audiomack", "Audiomack Trending consideration",
        action="Upload the track to your Audiomack creator account and request consideration",
        reason=REASON_ACCOUNT,
        destination_url="https://audiomack.com/creators",
        eligibility=[
            {"label": "Creator account exists", "ok": None},
            {"label": "Track uploaded", "ok": None},
            {"label": "Required metadata present",
             "ok": bool(recording["isrc"] and values.get("primary_genre"))},
        ],
        fields=answers,
        effort=EFFORT_QUICK,
    ))

    # --- Apple Music -------------------------------------------------------
    for title, action, effort in [
        ("Apple Music — distributor readiness",
         "Confirm with your distributor that the release is delivered to Apple Music "
         "with complete metadata and lyrics", EFFORT_QUICK),
        ("Apple Music — artist profile readiness",
         "Check Apple Music for Artists: profile image, bio and artist page are current",
         EFFORT_QUICK),
        ("Apple Music — editorial relationship outreach",
         "Ask your distributor or label contact to pass the release to their Apple "
         "editorial contact", EFFORT_MEDIUM),
        ("Apple Music — marketing assets",
         "Prepare the marketing assets Apple's editorial process expects", EFFORT_LONG),
    ]:
        created.append(create(
            campaign_id, "apple_music", title, action=action,
            reason="No automated Apple editorial pitching process exists. These are "
                   "relationship and readiness steps, not an automated submission.",
            destination_url="https://artists.apple.com",
            eligibility=[{"label": "Release delivered", "ok": delivered}],
            fields=answers, effort=effort,
        ))

    return [task_id for task_id in created if task_id]
