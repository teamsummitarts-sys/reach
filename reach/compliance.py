"""Communication compliance engine.

Every message gets a deterministic decision *record* before it can be queued —
not a judgement call made at send time. The record names the recipient country,
the contact category, the outreach category, where the address came from, the
lawful basis relied on, and every rule that fired.

This engine does not replace legal advice. Anything it cannot place cleanly
becomes LEGAL_REVIEW or a human action rather than a send.
"""

import json

from . import audit, clock, contacts, db, policy, rbac, scoring
from .errors import ValidationError

POLICY_VERSION = "compliance/1.0.0"

# --- decisions -------------------------------------------------------------

ALLOW = "ALLOW"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
DRAFT_ONLY = "DRAFT_ONLY"
LEGAL_REVIEW = "LEGAL_REVIEW"
BLOCK = "BLOCK"

DECISIONS = [ALLOW, APPROVAL_REQUIRED, DRAFT_ONLY, LEGAL_REVIEW, BLOCK]
_SEVERITY = {BLOCK: 4, LEGAL_REVIEW: 3, DRAFT_ONLY: 2, APPROVAL_REQUIRED: 1, ALLOW: 0}

# --- outreach categories ---------------------------------------------------

REQUESTED_SUBMISSION = "REQUESTED_SUBMISSION"
PUBLISHED_SUBMISSION_ADDRESS = "PUBLISHED_SUBMISSION_ADDRESS"
EXISTING_RELATIONSHIP = "EXISTING_RELATIONSHIP"
RELATIONSHIP_REPLY = "RELATIONSHIP_REPLY"
COLD_PROFESSIONAL_OUTREACH = "COLD_PROFESSIONAL_OUTREACH"
PAID_CONSIDERATION = "PAID_CONSIDERATION"
SPONSORSHIP_OR_ADVERTISING = "SPONSORSHIP_OR_ADVERTISING"
SOCIAL_DM_DRAFT = "SOCIAL_DM_DRAFT"
OUTREACH_UNKNOWN = "UNKNOWN"

OUTREACH_CATEGORIES = [
    REQUESTED_SUBMISSION, PUBLISHED_SUBMISSION_ADDRESS, EXISTING_RELATIONSHIP,
    RELATIONSHIP_REPLY, COLD_PROFESSIONAL_OUTREACH, PAID_CONSIDERATION,
    SPONSORSHIP_OR_ADVERTISING, SOCIAL_DM_DRAFT, OUTREACH_UNKNOWN,
]

# --- territory rules -------------------------------------------------------

EU_EEA = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
    "SI", "ES", "SE", "IS", "LI", "NO",
}

# Rough ccTLD → country hints. Used only to pick a rule set, never asserted as
# fact about a person.
_TLD_COUNTRY = {
    "uk": "GB", "de": "DE", "fr": "FR", "nl": "NL", "es": "ES", "it": "IT",
    "se": "SE", "no": "NO", "dk": "DK", "fi": "FI", "pl": "PL", "ie": "IE",
    "be": "BE", "at": "AT", "pt": "PT", "cz": "CZ", "ca": "CA", "au": "AU",
    "nz": "NZ", "jp": "JP", "br": "BR", "mx": "MX", "us": "US",
}

LAWFUL_BASIS = {
    "US": "CAN-SPAM: commercial email permitted with truthful headers, a clear "
          "business identity, a valid postal address and a working opt-out.",
    "CA": "CASL: relies on implied consent from a business address conspicuously "
          "published without a stated restriction, for a message relevant to the "
          "recipient's professional role.",
    "EU": "GDPR Art. 6(1)(f) legitimate interest for B2B outreach to a published "
          "professional role address, with transparency and an immediate right to "
          "object honoured on first request.",
    "UK": "UK GDPR legitimate interest plus PECR: corporate subscriber outreach to "
          "a published professional address, with an immediate opt-out.",
    "OTHER": "Professional outreach to an address the outlet published for exactly "
             "this purpose, with an immediate opt-out.",
}


def rule_set_for(country):
    if not country:
        return "OTHER"
    country = country.upper()
    if country == "US":
        return "US"
    if country == "CA":
        return "CA"
    if country == "GB":
        return "UK"
    if country in EU_EEA:
        return "EU"
    return "OTHER"


def infer_country(outlet=None, contact_domain=None):
    """Best-effort, and honest about it: returns None when unknown."""
    if outlet is not None and outlet["territory"]:
        return outlet["territory"].upper()
    domain = contact_domain or (outlet["domain"] if outlet is not None else None)
    if domain:
        tld = domain.rsplit(".", 1)[-1].lower()
        if tld in _TLD_COUNTRY:
            return _TLD_COUNTRY[tld]
    return None


def classify_outreach(contact_category, cost_model, relationship=None, mode=None):
    if relationship is not None and (relationship["accepted_count"] or
                                     relationship["placement_count"]):
        return EXISTING_RELATIONSHIP
    if cost_model in ("PAID_CONSIDERATION",):
        return PAID_CONSIDERATION
    if cost_model in ("ADVERTISING_OR_SPONSORSHIP",):
        return SPONSORSHIP_OR_ADVERTISING
    if contact_category == contacts.EXPLICIT_SUBMISSION_ROUTE:
        return PUBLISHED_SUBMISSION_ADDRESS
    if contact_category in (contacts.ROLE_BASED_ADDRESS, contacts.VERIFIED_PUBLIC_BUSINESS):
        return COLD_PROFESSIONAL_OUTREACH
    return OUTREACH_UNKNOWN


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------

def decide(target_id=None, contact_method_id=None, campaign=None, outlet=None,
           relationship=None, cost_model=None, risk=None, sender_health=None,
           tenant_id=None, persist=True):
    """Produce a :class:`ComplianceDecisionRecord`-shaped dict.

    The decision is the most restrictive outcome any rule produced. Rules never
    cancel each other out.
    """
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    reasons = []
    decision = APPROVAL_REQUIRED  # Copilot never sends without an approval.

    method = contacts.get_method(contact_method_id) if contact_method_id else None
    contact_category = method["category"] if method else contacts.UNKNOWN
    sendability = contacts.sendability(contact_method_id, tenant_id) if method else None

    recipient_country = infer_country(outlet, method["domain"] if method else None)
    sender_country = _sender_country()
    rules = rule_set_for(recipient_country)

    # --- absolute blocks ---------------------------------------------------
    if policy.send_stop_engaged():
        return _finalize(BLOCK, ["Emergency send stop is engaged"], locals(), persist)

    if method is None:
        reasons.append("No contact method resolved — this opportunity has no email route")
        decision = _worse(decision, DRAFT_ONLY)
    else:
        if method["spotify_only"]:
            reasons.append(
                "Address was found only through Spotify. Spotify data may not be used "
                "to resolve an outreach contact."
            )
            decision = _worse(decision, BLOCK)
        suppressed = contacts.is_suppressed(value_hash=method["value_hash"],
                                            tenant_id=tenant_id)
        if suppressed is not None:
            reasons.append(f"Recipient is suppressed ({suppressed['reason']})")
            decision = _worse(decision, BLOCK)
        if contact_category == contacts.PROHIBITED_PRIVATE_INFORMATION:
            reasons.append("Address is prohibited private information")
            decision = _worse(decision, BLOCK)
        if contact_category == contacts.PERSONAL_ADDRESS:
            reasons.append("Personal addresses are never used for outreach")
            decision = _worse(decision, BLOCK)
        if contact_category == contacts.DATA_BROKER_SOURCE:
            reasons.append("Address came from an unlicensed contact list")
            decision = _worse(decision, BLOCK)
        if sendability and not sendability["sendable"] and decision != BLOCK:
            for blocker in sendability["blockers"]:
                reasons.append(blocker)
            decision = _worse(decision, DRAFT_ONLY)

    # --- risk --------------------------------------------------------------
    if risk:
        if risk.get("band") == scoring.BLOCK:
            reasons.append(f"Risk score {risk['score']} — blocked")
            decision = _worse(decision, BLOCK)
        elif risk.get("band") == scoring.HIGH:
            reasons.append(f"Risk score {risk['score']} — legal review required")
            decision = _worse(decision, LEGAL_REVIEW)
        elif risk.get("band") == scoring.REVIEW:
            reasons.append(f"Risk score {risk['score']} — human review required")
            decision = _worse(decision, APPROVAL_REQUIRED)

    # --- commercial model --------------------------------------------------
    outreach_category = classify_outreach(contact_category, cost_model, relationship)
    if outreach_category == PAID_CONSIDERATION:
        reasons.append("Paid consideration — explicit spend approval required, and a fee "
                       "never implies placement")
        decision = _worse(decision, APPROVAL_REQUIRED)
    if outreach_category == SPONSORSHIP_OR_ADVERTISING:
        reasons.append("Advertising or sponsorship must be reviewed and disclosed")
        decision = _worse(decision, LEGAL_REVIEW)
    if cost_model in ("GUARANTEED_PLACEMENT", "GUARANTEED_STREAMS"):
        reasons.append(f"{cost_model.replace('_', ' ').title()} offers are blocked outright")
        decision = _worse(decision, BLOCK)
    if outreach_category == SOCIAL_DM_DRAFT:
        reasons.append("Automated social messages are a Phase One non-goal — draft only")
        decision = _worse(decision, DRAFT_ONLY)

    # --- territory rules ---------------------------------------------------
    if recipient_country is None:
        reasons.append("Recipient country is UNKNOWN — the most restrictive rule set applies")
        decision = _worse(decision, APPROVAL_REQUIRED)
    if rules in ("EU", "UK"):
        reasons.append(
            "EU/UK rules: first contact must identify the sender, state where the "
            "address was found, and honour an objection immediately"
        )
    elif rules == "CA":
        reasons.append("CASL: implied consent expires — re-contact limits are enforced")
    elif rules == "US":
        reasons.append("CAN-SPAM: postal address and a working opt-out are required")

    # --- sender health -----------------------------------------------------
    if sender_health is not None and not sender_health.get("ready"):
        reasons.append("Sender health has not passed — outreach is drafts only")
        decision = _worse(decision, DRAFT_ONLY)

    # --- campaign mode -----------------------------------------------------
    if campaign is not None and campaign["mode"] == "SCOUT":
        reasons.append("Scout mode never sends")
        decision = _worse(decision, DRAFT_ONLY)

    return _finalize(decision, reasons, locals(), persist)


def _worse(current, candidate):
    return candidate if _SEVERITY[candidate] > _SEVERITY[current] else current


def _sender_country():
    from . import config

    return config.sender_country()


def _finalize(decision, reasons, scope, persist):
    method = scope.get("method")
    record = {
        "contactId": method["contact_id"] if method else None,
        "contactMethodId": method["id"] if method else None,
        "campaignId": scope["campaign"]["id"] if scope.get("campaign") is not None else None,
        "targetId": scope.get("target_id"),
        "decision": decision,
        "recipientCountry": scope.get("recipient_country"),
        "senderCountry": scope.get("sender_country") or _sender_country(),
        "contactCategory": scope.get("contact_category", contacts.UNKNOWN),
        "outreachCategory": scope.get("outreach_category", OUTREACH_UNKNOWN),
        "sourceOfAddress": _source_of_address(method),
        "permissionSignal": _permission_signal(scope.get("contact_category")),
        "lawfulBasisRecord": LAWFUL_BASIS.get(
            rule_set_for(scope.get("recipient_country")), LAWFUL_BASIS["OTHER"]),
        "suppressionChecked": method is not None,
        "providerPolicyChecked": True,
        "senderHealthChecked": scope.get("sender_health") is not None,
        "reasons": reasons,
        "policyVersion": POLICY_VERSION,
        "decidedAt": clock.now_iso(),
    }

    if persist:
        tenant_id = scope.get("tenant_id") or rbac.current_principal().tenant_id
        decision_id = db.new_id("cd")
        db.insert("compliance_decision", {
            "id": decision_id,
            "tenant_id": tenant_id,
            "target_id": scope.get("target_id"),
            "campaign_id": record["campaignId"],
            "contact_id": record["contactId"],
            "decision": decision,
            "record_json": json.dumps(record, default=str),
            "policy_version": POLICY_VERSION,
            "decided_at": record["decidedAt"],
        })
        record["id"] = decision_id
        audit.record("compliance.decided", entity_type="campaign_target",
                     entity_id=scope.get("target_id"),
                     payload={"decision": decision, "reasons": reasons[:5]})
    return record


def _source_of_address(method):
    if method is None:
        return "NONE"
    rows = contacts.method_evidence(method["id"])
    if not rows:
        return "UNRECORDED"
    return rows[0]["source_url"]


def _permission_signal(category):
    if category == contacts.EXPLICIT_SUBMISSION_ROUTE:
        return "Outlet published this address specifically for music submissions"
    if category == contacts.ROLE_BASED_ADDRESS:
        return "Role-based business address published by the outlet"
    if category == contacts.VERIFIED_PUBLIC_BUSINESS:
        return "Business address corroborated by independent sources"
    return None


def latest_decision(target_id):
    return db.query_one(
        "SELECT * FROM compliance_decision WHERE target_id = ? "
        "ORDER BY decided_at DESC LIMIT 1",
        (target_id,),
    )


def decision_record(row):
    if row is None:
        return None
    try:
        return json.loads(row["record_json"])
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# opt-out handling
# --------------------------------------------------------------------------

def record_opt_out(value, source="reply", tenant_id=None):
    """An objection is honoured immediately and permanently."""
    contacts.suppress(value, "Opt-out requested by recipient", scope=contacts.GLOBAL,
                      source=source)
    audit.record("compliance.opt_out", entity_type="contact", entity_id=None,
                 payload={"source": source})
    return True


def record_bounce(value, permanent=True, source="webhook"):
    if permanent:
        contacts.suppress(value, "Permanent bounce", scope=contacts.GLOBAL, source=source)
    audit.record("compliance.bounce", entity_type="contact", entity_id=None,
                 payload={"permanent": permanent, "source": source})
    return permanent


def unsubscribe_footer(sender_name, postal_address, source_url=None):
    """Required disclosure text, built from real configuration.

    Raises rather than inventing a postal address, because a fabricated one is
    worse than no send at all.
    """
    if not postal_address:
        raise ValidationError(
            "A physical postal address is required in outreach. Set "
            "REACH_SENDER_POSTAL_ADDRESS."
        )
    lines = [
        f"{sender_name} · {postal_address}",
    ]
    if source_url:
        lines.append(f"We found your submission details at {source_url}.")
    lines.append(
        "If you'd rather not hear from us, reply with 'unsubscribe' and we will not "
        "contact you again."
    )
    return "\n".join(lines)
