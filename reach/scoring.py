"""REACH score and risk score.

Both are transparent: every component stores its own value, and the reasons
shown in the UI are generated from the same numbers the score was built from.
Follower count is deliberately a small input — a small, active, precisely
relevant outlet should outrank a large generic playlist, and the weights say so.
"""

import json

from . import clock, db, evidence, extractor
from .errors import ValidationError

REACH_SCORE_VERSION = "reach-score/1.0.0"
RISK_SCORE_VERSION = "risk-score/1.0.0"

# component -> weight. They sum to 1.0.
COMPONENT_WEIGHTS = {
    "genre_fit": 0.16,
    "microgenre_fit": 0.14,
    "context_fit": 0.08,
    "territory_fit": 0.07,
    "audience_fit": 0.05,
    "opportunity_activity": 0.10,
    "credibility": 0.08,
    "submission_availability": 0.12,
    "contact_verification": 0.10,
    "relationship_history": 0.06,
    "cost_efficiency": 0.02,
    "freshness": 0.02,
}

COMPONENT_LABELS = {
    "genre_fit": "Genre match",
    "microgenre_fit": "Microgenre match",
    "context_fit": "Mood / playlist context match",
    "territory_fit": "Target territory match",
    "audience_fit": "Audience fit",
    "opportunity_activity": "Opportunity is active",
    "credibility": "Outlet credibility",
    "submission_availability": "Explicit submission route",
    "contact_verification": "Contact verification",
    "relationship_history": "Relationship history",
    "cost_efficiency": "Cost efficiency",
    "freshness": "Source freshness",
}


def _overlap(left, right):
    left_set = {str(item).strip().lower() for item in (left or []) if str(item).strip()}
    right_set = {str(item).strip().lower() for item in (right or []) if str(item).strip()}
    if not left_set or not right_set:
        return None  # UNKNOWN, not zero
    direct = left_set & right_set
    if direct:
        return min(1.0, len(direct) / min(len(left_set), 3))
    # Partial credit for a substring match ("dark electronic" vs "electronic").
    for item in left_set:
        for other in right_set:
            if item in other or other in item:
                return 0.5
    return 0.0


def score_opportunity(outlet, profile_values, campaign, contact_state=None,
                      relationship=None, freshness_days=None):
    """Compute the 0–100 REACH score with per-component detail."""
    components = {}
    reasons = []

    genres = json.loads(outlet["genres_json"]) if outlet["genres_json"] else []
    profile_genres = [profile_values.get("primary_genre")] + list(
        profile_values.get("secondary_genres") or [])
    microgenres = profile_values.get("microgenres") or []

    components["genre_fit"] = _overlap(profile_genres, genres)
    components["microgenre_fit"] = _overlap(microgenres, genres)
    components["context_fit"] = _overlap(
        (profile_values.get("mood") or []) + (profile_values.get("playlist_contexts") or []),
        genres + ([outlet["description"]] if outlet["description"] else []),
    )

    territories = json.loads(campaign["territories_json"]) if campaign["territories_json"] else []
    if outlet["territory"] and territories:
        components["territory_fit"] = 1.0 if outlet["territory"] in territories else 0.2
    else:
        components["territory_fit"] = None

    components["audience_fit"] = _audience_fit(outlet)
    components["opportunity_activity"] = _activity(outlet)
    components["credibility"] = _credibility(outlet)
    components["submission_availability"] = _submission_availability(outlet)
    components["contact_verification"] = _contact_verification(contact_state)
    components["relationship_history"] = _relationship(relationship)
    components["cost_efficiency"] = _cost_efficiency(outlet)
    components["freshness"] = _freshness(freshness_days)

    known = {key: value for key, value in components.items() if value is not None}
    if not known:
        total = 0
    else:
        weight_sum = sum(COMPONENT_WEIGHTS[key] for key in known)
        weighted = sum(COMPONENT_WEIGHTS[key] * value for key, value in known.items())
        total = round(weighted / weight_sum * 100) if weight_sum else 0

    for key, value in sorted(components.items(), key=lambda pair: -COMPONENT_WEIGHTS[pair[0]]):
        label = COMPONENT_LABELS[key]
        if value is None:
            reasons.append({"sign": "?", "text": f"{label} unverified"})
        elif value >= 0.75:
            reasons.append({"sign": "+", "text": _positive_reason(key, value, outlet)})
        elif value <= 0.25:
            reasons.append({"sign": "-", "text": _negative_reason(key, value, outlet)})

    return {
        "score": total,
        "components": components,
        "reasons": reasons[:8],
        "version": REACH_SCORE_VERSION,
    }


def _positive_reason(key, value, outlet):
    return {
        "genre_fit": "Genre match with the track profile",
        "microgenre_fit": "Strong microgenre match",
        "context_fit": "Mood and playlist context align",
        "territory_fit": "Target territory match",
        "audience_fit": "Audience size is reported by the outlet",
        "opportunity_activity": "Updated recently",
        "credibility": "Credible, identifiable outlet",
        "submission_availability": "Explicit submission route published",
        "contact_verification": "Contact independently verified",
        "relationship_history": "Previously accepted related music",
        "cost_efficiency": "Free submission",
        "freshness": "Source checked recently",
    }[key]


def _negative_reason(key, value, outlet):
    return {
        "genre_fit": "Weak genre match",
        "microgenre_fit": "No microgenre overlap",
        "context_fit": "Mood and context do not align",
        "territory_fit": "Outside the campaign's target territories",
        "audience_fit": "Audience size unverified",
        "opportunity_activity": "No recent activity",
        "credibility": "Ownership of this outlet is unclear",
        "submission_availability": "No published submission route",
        "contact_verification": "Contact not independently verified",
        "relationship_history": "No prior relationship",
        "cost_efficiency": "Paid consideration required",
        "freshness": "Source has not been re-checked",
    }[key]


def _audience_fit(outlet):
    """Follower count carries little weight and only when its source is known."""
    if outlet["follower_count"] is None or not outlet["follower_count_source"]:
        return None
    count = outlet["follower_count"]
    if count >= 100_000:
        return 0.9
    if count >= 10_000:
        return 0.8
    if count >= 1_000:
        return 0.7
    return 0.5


def _activity(outlet):
    days = clock.days_since(outlet["last_activity_at"]) if outlet["last_activity_at"] else None
    if days is None:
        return None
    if days <= 14:
        return 1.0
    if days <= 45:
        return 0.8
    if days <= 120:
        return 0.5
    if days <= 365:
        return 0.25
    return 0.0


def _credibility(outlet):
    score = 0.0
    if outlet["organization_id"]:
        score += 0.4
    if outlet["url"]:
        score += 0.2
    if outlet["description"]:
        score += 0.2
    corroborating = evidence.corroboration("outlet", outlet["id"], "classification")
    if corroborating >= 2:
        score += 0.2
    return min(1.0, score)


def _submission_availability(outlet):
    row = db.query_one(
        "SELECT * FROM submission_route WHERE outlet_id = ? AND active = 1 "
        "ORDER BY verified_at DESC LIMIT 1",
        (outlet["id"],),
    )
    if row is None:
        return 0.0 if outlet["submissions_open"] == extractor.CLOSED else None
    if outlet["submissions_open"] == extractor.CLOSED:
        return 0.0
    if row["method"] == "EMAIL" and outlet["submissions_open"] == extractor.OPEN:
        return 1.0
    if row["requires_login"] or row["requires_captcha"]:
        return 0.4
    return 0.7


def _contact_verification(contact_state):
    if not contact_state:
        return None
    if contact_state.get("sendable"):
        return 1.0
    if contact_state.get("category") in ("UNVERIFIED_PUBLIC_BUSINESS",):
        return 0.4
    return 0.1


def _relationship(relationship):
    if relationship is None:
        return None
    accepted = relationship["accepted_count"] or 0
    placements = relationship["placement_count"] or 0
    declined = relationship["declined_count"] or 0
    if placements:
        return 1.0
    if accepted:
        return 0.85
    if declined:
        return 0.2
    return 0.5


def _cost_efficiency(outlet):
    row = db.query_one(
        "SELECT cost_model, cost_amount FROM submission_route WHERE outlet_id = ? "
        "AND active = 1 ORDER BY created_at DESC LIMIT 1",
        (outlet["id"],),
    )
    if row is None:
        return None
    if row["cost_model"] == extractor.FREE_SUBMISSION:
        return 1.0
    if row["cost_model"] in (extractor.GUARANTEED_PLACEMENT_COST,
                             extractor.GUARANTEED_STREAMS_COST):
        return 0.0
    if row["cost_model"] == extractor.PAID_CONSIDERATION_COST:
        amount = row["cost_amount"] or 0
        return 0.4 if amount and amount <= 20 else 0.2
    return None


def _freshness(freshness_days):
    if freshness_days is None:
        return None
    if freshness_days <= 7:
        return 1.0
    if freshness_days <= 30:
        return 0.8
    if freshness_days <= 90:
        return 0.5
    return 0.2


def persist_score(target_id, result):
    score_id = db.new_id("score")
    db.insert("opportunity_score", {
        "id": score_id,
        "target_id": target_id,
        "score": result["score"],
        "components_json": json.dumps(result["components"]),
        "reasons_json": json.dumps(result["reasons"]),
        "version": result["version"],
        "human_override": 0,
        "override_reason": None,
        "created_at": clock.now_iso(),
    })
    return score_id


def override_score(target_id, score, reason):
    from . import audit, rbac

    rbac.require("target.edit")
    if not reason or not reason.strip():
        raise ValidationError("A score override requires a reason")
    if not 0 <= score <= 100:
        raise ValidationError("Score must be between 0 and 100")
    score_id = db.new_id("score")
    db.insert("opportunity_score", {
        "id": score_id,
        "target_id": target_id,
        "score": int(score),
        "components_json": json.dumps({}),
        "reasons_json": json.dumps([{"sign": "!", "text": f"Human override: {reason}"}]),
        "version": REACH_SCORE_VERSION,
        "human_override": 1,
        "override_reason": reason,
        "created_at": clock.now_iso(),
    })
    audit.record("score.overridden", entity_type="campaign_target", entity_id=target_id,
                 payload={"score": score, "reason": reason}, actor_kind=audit.ACTOR_USER)
    return score_id


def latest_score(target_id):
    # rowid breaks the tie when two scores land in the same second — an
    # override recorded immediately after a computed score must win.
    return db.query_one(
        "SELECT * FROM opportunity_score WHERE target_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (target_id,),
    )


# --------------------------------------------------------------------------
# risk
# --------------------------------------------------------------------------

LOW = "LOW RISK"
REVIEW = "REVIEW"
HIGH = "HIGH RISK"
BLOCK = "BLOCK"

RISK_SIGNALS = {
    "guaranteed_streams": (60, "Guaranteed streams offered"),
    "guaranteed_placement": (55, "Guaranteed placement offered"),
    "artificial_streaming_language": (40, "Language associated with artificial streaming"),
    "unrealistic_claims": (25, "Unrealistic performance claims"),
    "unexplained_payment": (20, "Payment required without a stated service"),
    "incoherent_programming": (15, "Programming is unrelated or incoherent"),
    "copied_contact_infrastructure": (18, "Contact route copied from another unverified source"),
    "deceptive_identity": (30, "Outlet identity appears deceptive"),
    "stale_presence": (10, "Presence looks abandoned"),
    "unclear_ownership": (12, "Ownership is unclear"),
    "missing_submission_terms": (8, "No submission terms published"),
    "injection_attempt": (35, "Page contained content designed to manipulate an automated agent"),
    "honeypot_contact": (25, "Contact address hidden from human readers"),
}


def assess_risk(extracted, outlet=None, contact_state=None):
    """Independent of the REACH score. UNKNOWN never means safe."""
    signals = []
    classification = extracted.get("classification")

    if classification == extractor.GUARANTEED_STREAMS:
        signals.append("guaranteed_streams")
    if classification == extractor.GUARANTEED_PLACEMENT:
        signals.append("guaranteed_placement")

    text = (extracted.get("classification_excerpt") or "") + " " + (extracted.get("title") or "")
    lowered = text.lower()
    if any(phrase in lowered for phrase in ("buy streams", "stream package", "plays package",
                                            "organic streams guaranteed")):
        signals.append("artificial_streaming_language")
    if any(phrase in lowered for phrase in ("400 playlists", "network of", "million streams",
                                            "overnight", "instant results")):
        signals.append("unrealistic_claims")

    if extracted.get("cost_model") == extractor.PAID_CONSIDERATION_COST and not extracted.get("cost_amount"):
        signals.append("unexplained_payment")
    if extracted.get("injection_findings"):
        signals.append("injection_attempt")
    if extracted.get("hidden_emails"):
        signals.append("honeypot_contact")
    if extracted.get("submissions_open") == extractor.SUBMISSIONS_UNKNOWN:
        signals.append("missing_submission_terms")
    if not extracted.get("outlet_name") or extracted.get("outlet_name") == extracted.get("domain"):
        signals.append("unclear_ownership")

    freshness_date = (extracted.get("freshness") or {}).get("date")
    if freshness_date:
        days = clock.days_since(freshness_date if len(freshness_date) > 4
                                else f"{freshness_date}-01-01")
        if days is not None and days > 730:
            signals.append("stale_presence")

    if contact_state and contact_state.get("category") == "PROHIBITED_PRIVATE_INFORMATION":
        signals.append("honeypot_contact")

    score = min(100, sum(RISK_SIGNALS[name][0] for name in set(signals)))
    band = risk_band(score)
    return {
        "score": score,
        "band": band,
        "signals": [{"key": name, "weight": RISK_SIGNALS[name][0],
                     "label": RISK_SIGNALS[name][1]} for name in sorted(set(signals))],
        "version": RISK_SCORE_VERSION,
        # Never an accusation, always an observation.
        "statement": "Potential risk indicators detected." if signals
                     else "No risk indicators detected in the evidence reviewed.",
    }


def risk_band(score):
    if score >= 75:
        return BLOCK
    if score >= 50:
        return HIGH
    if score >= 21:
        return REVIEW
    return LOW


def persist_risk(target_id, result):
    risk_id = db.new_id("risk")
    db.insert("risk_assessment", {
        "id": risk_id,
        "target_id": target_id,
        "score": result["score"],
        "band": result["band"],
        "signals_json": json.dumps(result["signals"]),
        "version": result["version"],
        "created_at": clock.now_iso(),
    })
    return risk_id


def latest_risk(target_id):
    return db.query_one(
        "SELECT * FROM risk_assessment WHERE target_id = ? ORDER BY created_at DESC LIMIT 1",
        (target_id,),
    )
