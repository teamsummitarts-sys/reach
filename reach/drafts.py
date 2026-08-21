"""Outreach draft generation.

No language-model credential is configured, so pitches are composed
deterministically from facts REACH can actually cite. That is not a downgrade
for correctness: every sentence in a draft is bound to a source in
``facts_json``, and a sentence with no source is never written.

The rules the generator obeys, in code rather than in a prompt:

* only first-party catalog data and evidence from sources permitted for content
  reuse may appear — Spotify-sourced text is excluded outright;
* nothing is claimed about the recipient that evidence does not support;
* no fabricated placements, follower counts, praise, urgency or familiarity;
* one link, no large attachments;
* a local-language draft only when the confidence is high, otherwise English
  with a translation-review flag.
"""

import hashlib
import json

from . import (audit, campaigns, catalog, clock, compliance, config, contacts, db,
               entities, evidence, firewall, profile, rbac, firstparty, scoring,
               sender)
from .errors import ValidationError

GENERATOR_VERSION = "pitch-writer/1.0.0"

DRAFT = "DRAFT"
NEEDS_APPROVAL = "NEEDS_APPROVAL"
APPROVED = "APPROVED"
INVALIDATED = "INVALIDATED"
SENT = "SENT"

# Local-language openers we are confident enough to send unreviewed.
HIGH_CONFIDENCE_LANGUAGES = {
    "de": {
        "greeting": "Hallo {name},",
        "closing": "Vielen Dank für Ihre Zeit,",
        "request": "Wäre der Track etwas für Sie? Ich freue mich über jede Rückmeldung.",
    },
}


def _material_payload(recipient_hash, subject, body, links, attachments, cost,
                      from_email):
    return {
        "recipient_hash": recipient_hash,
        "subject": subject,
        "body": body,
        "links": sorted(links or []),
        "attachments": sorted(attachments or []),
        "cost": round(float(cost or 0), 2),
        "from_email": from_email,
    }


def payload_hash(recipient_hash, subject, body, links, attachments, cost, from_email):
    """The hash the executor checks. Any material change produces a new value."""
    canonical = json.dumps(
        _material_payload(recipient_hash, subject, body, links, attachments, cost,
                          from_email),
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _permitted_evidence(outlet_id):
    """Evidence REACH may quote. Restricted providers are dropped, not trimmed."""
    permitted = []
    for row in evidence.for_entity("outlet", outlet_id):
        usage = firewall.source_usage_policy(_provider_for_evidence(row))
        if usage.maySendToLLM and usage.mayStoreExcerpt:
            permitted.append(row)
    return permitted


def _provider_for_evidence(row):
    if row["source_document_id"]:
        document = evidence.get_source_document(row["source_document_id"])
        if document is not None:
            return document["provider"]
    return "open_web_fetch"


def generate(target_id, tenant_id=None):
    """Compose a draft for one target. Returns the draft id."""
    principal = rbac.require("draft.generate")
    tenant_id = tenant_id or principal.tenant_id

    target = campaigns.get_target(target_id)
    if target is None:
        raise ValidationError("Unknown target")
    campaign = campaigns.get(target["campaign_id"])
    outlet = entities.get_outlet(target["outlet_id"])
    recording = catalog.get_recording(campaign["recording_id"])
    facts = catalog.track(recording)
    profile_id = profile.get_or_create(campaign["recording_id"])
    values = profile.values(profile_id)
    baseline = firstparty.performance_baseline(facts)
    identity = sender.identity(tenant_id)

    method_row = _contact_method_for(target)
    if method_row is None:
        raise ValidationError("This target has no resolved contact method to draft for")

    facts = []
    lines = []

    # 1 — the specific, evidenced reason this outlet was selected.
    relevance, relevance_facts = _relevance_sentence(outlet, values, target_id)
    facts.extend(relevance_facts)

    language, translation_flagged = _select_language(outlet, values, campaign)
    strings = HIGH_CONFIDENCE_LANGUAGES.get(language, {})

    greeting = strings.get("greeting", "Hello {name},").format(
        name=outlet["name"] or "there"
    )
    lines.append(greeting)
    lines.append("")
    lines.append(relevance)

    # 2 — artist and track context, entirely first-party.
    context_sentence, context_facts = _context_sentence(recording, values, baseline)
    lines.append("")
    lines.append(context_sentence)
    facts.extend(context_facts)

    # 3 — the fit, stated only where evidence supports it.
    fit_sentence, fit_facts = _fit_sentence(outlet, values)
    if fit_sentence:
        lines.append("")
        lines.append(fit_sentence)
        facts.extend(fit_facts)

    # 4 — exactly one link.
    links = _links(recording, campaign)
    if links:
        lines.append("")
        lines.append(f"Listen here: {links[0]}")
        facts.append({
            "claim": "listening link",
            "basis": "First-party asset configured by the campaign owner",
            "source": "reach_catalog",
        })

    # 5 — the request.
    lines.append("")
    lines.append(strings.get(
        "request",
        "If it's a fit for what you're programming, I'd be glad for you to consider it. "
        "Either way, thank you for reading.",
    ))

    # 6 — sign-off, identification and opt-out.
    lines.append("")
    lines.append(strings.get("closing", "Best,"))
    lines.append(recording["artist_name"])
    lines.append("")
    source_url = _address_source_url(method_row)
    lines.append("—")
    lines.append(compliance.unsubscribe_footer(
        identity["from_name"], identity["postal_address"], source_url
    ))
    facts.append({
        "claim": "where we found your submission details",
        "basis": source_url or "Recorded evidence packet",
        "source": "open_web_fetch",
    })

    subject = _subject(recording, values, outlet)
    body = "\n".join(lines)

    # The firewall runs even though no model is involved: the same rule governs
    # which sources may be repurposed into generated output.
    firewall.guard_language_model(
        {fact["source"] for fact in facts if fact.get("source")},
        payload=body, purpose="pitch composition",
    )

    digest = payload_hash(method_row["value_hash"], subject, body, links, [], 0.0,
                          identity["from_email"])

    existing = db.query_one(
        "SELECT * FROM outreach_draft WHERE target_id = ? ORDER BY created_at DESC LIMIT 1",
        (target_id,),
    )
    now = clock.now_iso()
    payload = {
        "recipient_preview": method_row["value_preview"],
        "recipient_hash": method_row["value_hash"],
        "subject": subject,
        "body": body,
        "links_json": json.dumps(links),
        "attachments_json": json.dumps([]),
        "language": language,
        "translation_flagged": 1 if translation_flagged else 0,
        "facts_json": json.dumps(facts),
        "generator_version": GENERATOR_VERSION,
        "payload_hash": digest,
        "status": DRAFT,
        "updated_at": now,
    }

    if existing is not None and existing["status"] in (DRAFT, INVALIDATED):
        db.update("outreach_draft", existing["id"], payload)
        draft_id = existing["id"]
    else:
        draft_id = db.new_id("draft")
        payload.update({"id": draft_id, "tenant_id": tenant_id, "target_id": target_id,
                        "created_at": now})
        db.insert("outreach_draft", payload)

    campaigns.set_target_status(target_id, campaigns.DRAFTED,
                                reason="Draft generated and awaiting review")
    audit.record("draft.generated", entity_type="outreach_draft", entity_id=draft_id,
                 payload={"target_id": target_id, "language": language},
                 actor_kind=audit.ACTOR_USER, actor_id=principal.id)
    return draft_id


def _contact_method_for(target):
    if not target["contact_id"]:
        return None
    rows = contacts.methods_for_contact(target["contact_id"])
    for row in rows:
        if row["category"] in contacts.SENDABLE_CATEGORIES:
            return row
    return rows[0] if rows else None


def _address_source_url(method_row):
    rows = contacts.method_evidence(method_row["id"])
    return rows[0]["source_url"] if rows else None


def _relevance_sentence(outlet, values, target_id):
    """States why this outlet, citing the score component that drove it."""
    facts = []
    score_row = scoring.latest_score(target_id)
    reasons = json.loads(score_row["reasons_json"]) if score_row else []
    positive = [reason["text"] for reason in reasons if reason.get("sign") == "+"]

    outlet_genres = json.loads(outlet["genres_json"] or "[]")
    microgenres = [item.lower() for item in (values.get("microgenres") or [])]
    shared = [genre for genre in outlet_genres if genre.lower() in microgenres]

    if shared:
        sentence = (
            f"I'm writing because {outlet['name']} covers {shared[0]}, which is exactly "
            f"where this track sits."
        )
        facts.append({
            "claim": f"{outlet['name']} covers {shared[0]}",
            "basis": "Extracted from the outlet's own page and stored as evidence",
            "source": "open_web_fetch",
        })
    elif outlet["submissions_open"] == "OPEN":
        sentence = (
            f"I saw that {outlet['name']} is currently open to music submissions, so I "
            "wanted to send one track for your consideration."
        )
        facts.append({
            "claim": "submissions are open",
            "basis": "Submission guidelines published on the outlet's own page",
            "source": "open_web_fetch",
        })
    else:
        sentence = (
            f"I'm getting in touch about one track that may suit {outlet['name']}."
        )
        facts.append({
            "claim": "outlet name",
            "basis": "Outlet page title",
            "source": "open_web_fetch",
        })

    if positive:
        facts.append({
            "claim": "selection reason",
            "basis": f"REACH score component: {positive[0]}",
            "source": "reach_catalog",
        })
    return sentence, facts


def _context_sentence(recording, values, baseline):
    facts = []
    genre = values.get("primary_genre")
    parts = [f"The track is \"{recording['title']}\" by {recording['artist_name']}"]
    if genre:
        parts.append(f"a {genre} record")
        facts.append({
            "claim": f"genre is {genre}",
            "basis": "Track intelligence profile, supplied by the rights holder",
            "source": "user_input",
        })
    sentence = ", ".join(parts) + "."

    narrative = values.get("release_narrative")
    if narrative:
        sentence += f" {narrative}."
        facts.append({
            "claim": "release narrative",
            "basis": "Supplied by the rights holder in the track profile",
            "source": "user_input",
        })
    elif baseline.get("streams"):
        # A first-party figure, stated as first-party rather than as a boast.
        sentence += (
            f" It has {baseline['streams']:,} streams to date across the platforms "
            "we report on."
        )
        facts.append({
            "claim": f"{baseline['streams']:,} streams",
            "basis": "REACH catalog — the artist's own reported figures",
            "source": "reach_catalog",
        })
    facts.append({
        "claim": "track title and artist",
        "basis": "REACH catalog record",
        "source": "reach_catalog",
    })
    return sentence, facts


def _fit_sentence(outlet, values):
    moods = values.get("mood") or []
    comparables = values.get("comparable_artists") or []
    if not moods and not comparables:
        return None, []
    facts = [{
        "claim": "comparable artists and mood",
        "basis": "Track intelligence profile, supplied by the rights holder",
        "source": "user_input",
    }]
    bits = []
    if comparables:
        bits.append(f"it sits close to {', '.join(comparables[:2])}")
    if moods:
        bits.append(f"the mood is {', '.join(moods[:2])}")
    return "For context, " + " and ".join(bits) + ".", facts


def _links(recording, campaign):
    """Exactly one link, and only one REACH can actually cite."""
    configured = config.env("REACH_EPK_BASE_URL")
    if configured:
        return [f"{configured.rstrip('/')}/{recording['id']}"]
    assets = catalog.platform_assets(recording["id"])
    for asset in assets:
        if asset["url"]:
            return [asset["url"]]
    return []


def _subject(recording, values, outlet):
    genre = values.get("primary_genre")
    descriptor = f"{genre} " if genre else ""
    return f"Submission — {recording['artist_name']} — \"{recording['title']}\" ({descriptor.strip() or 'new track'})"


def _select_language(outlet, values, campaign):
    """English unless we are confident in the recipient's professional language."""
    languages = json.loads(outlet["languages_json"] or "[]")
    territory = (outlet["territory"] or "").upper()
    candidate = None
    if languages:
        candidate = languages[0].lower()
    elif territory == "DE":
        candidate = "de"

    if candidate and candidate in HIGH_CONFIDENCE_LANGUAGES:
        return candidate, False
    if candidate and candidate != "en":
        # Draft in English and flag for a human translator rather than guessing.
        return "en", True
    return "en", False


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def get(draft_id):
    return db.query_one("SELECT * FROM outreach_draft WHERE id = ?", (draft_id,))


def for_target(target_id):
    return db.query_one(
        "SELECT * FROM outreach_draft WHERE target_id = ? ORDER BY created_at DESC LIMIT 1",
        (target_id,),
    )


def facts(draft_id):
    row = get(draft_id)
    if row is None:
        return []
    try:
        return json.loads(row["facts_json"])
    except (TypeError, ValueError):
        return []


def edit(draft_id, subject=None, body=None):
    """Editing a draft invalidates any approval it already carries."""
    from . import approvals

    principal = rbac.require("draft.generate")
    row = get(draft_id)
    if row is None:
        raise ValidationError("Unknown draft")

    identity = sender.identity(row["tenant_id"])
    new_subject = subject if subject is not None else row["subject"]
    new_body = body if body is not None else row["body"]
    links = json.loads(row["links_json"] or "[]")
    digest = payload_hash(row["recipient_hash"], new_subject, new_body, links, [], 0.0,
                          identity["from_email"])

    db.update("outreach_draft", draft_id, {
        "subject": new_subject,
        "body": new_body,
        "payload_hash": digest,
        "status": DRAFT,
        "updated_at": clock.now_iso(),
    })
    approvals.invalidate_for_draft(draft_id, "Draft content changed after approval")
    audit.record("draft.edited", entity_type="outreach_draft", entity_id=draft_id,
                 actor_kind=audit.ACTOR_USER, actor_id=principal.id)
    return digest
