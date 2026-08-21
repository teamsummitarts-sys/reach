"""Contact and submission-route resolution.

The rule that shapes this module: a contact is only usable when REACH can say
*where it was published* and *that the publication is a professional submission
route*. Everything else is recorded as an opportunity without a sendable route,
and becomes a human action instead.

Hard blocks enforced here:

* an address seen only through Spotify is never sendable;
* an address that appears only in hidden markup (a honeypot) is never sendable;
* a personal address is never sendable;
* an address is never guessed from a name and a company domain;
* a suppressed address is never sendable, ever again.
"""

import re

from . import audit, clock, crypto, db, evidence, extractor, rbac
from .errors import ValidationError

# --- categories ------------------------------------------------------------

EXPLICIT_SUBMISSION_ROUTE = "EXPLICIT_SUBMISSION_ROUTE"
VERIFIED_PUBLIC_BUSINESS = "VERIFIED_PUBLIC_BUSINESS"
UNVERIFIED_PUBLIC_BUSINESS = "UNVERIFIED_PUBLIC_BUSINESS"
ROLE_BASED_ADDRESS = "ROLE_BASED_ADDRESS"
PROFESSIONAL_SOCIAL_ROUTE = "PROFESSIONAL_SOCIAL_ROUTE"
PERSONAL_ADDRESS = "PERSONAL_ADDRESS"
SPOTIFY_ONLY_SOURCE = "SPOTIFY_ONLY_SOURCE"
DATA_BROKER_SOURCE = "DATA_BROKER_SOURCE"
PROHIBITED_PRIVATE_INFORMATION = "PROHIBITED_PRIVATE_INFORMATION"
UNKNOWN = "UNKNOWN"

CATEGORIES = [
    EXPLICIT_SUBMISSION_ROUTE, VERIFIED_PUBLIC_BUSINESS, UNVERIFIED_PUBLIC_BUSINESS,
    ROLE_BASED_ADDRESS, PROFESSIONAL_SOCIAL_ROUTE, PERSONAL_ADDRESS,
    SPOTIFY_ONLY_SOURCE, DATA_BROKER_SOURCE, PROHIBITED_PRIVATE_INFORMATION, UNKNOWN,
]

# Categories a direct email may ever be sent to. Note what is absent.
SENDABLE_CATEGORIES = {
    EXPLICIT_SUBMISSION_ROUTE, VERIFIED_PUBLIC_BUSINESS, ROLE_BASED_ADDRESS,
}

# --- methods ---------------------------------------------------------------

EMAIL = "EMAIL"
WEB_FORM = "WEB_FORM"
PLATFORM_PORTAL = "PLATFORM_PORTAL"
SOCIAL_PROFILE = "SOCIAL_PROFILE"
DISTRIBUTOR = "DISTRIBUTOR"
LOGIN_REQUIRED = "LOGIN_REQUIRED"
UNSUPPORTED = "UNSUPPORTED"

_FREEMAIL_LABELS = {
    "gmail", "googlemail", "yahoo", "hotmail", "outlook", "live", "msn",
    "icloud", "me", "aol", "gmx", "web", "mail", "proton", "protonmail",
    "yandex", "zoho", "fastmail", "hushmail", "inbox", "qq", "163",
}

_PERSONAL_NAME_RE = re.compile(r"^[a-z]+[._-][a-z]+\d*$")


def freemail_domain(domain):
    label = (domain or "").split(".")[0].lower()
    return label in _FREEMAIL_LABELS


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

def classify(value, page_classification=None, provider="open_web_fetch",
             origin="visible-text", role_based=None, independent_domains=0,
             hidden_only=False, page_is_official=False):
    """Decide what kind of contact this is, and why.

    Returns ``(category, reasons)``. The reasons are shown verbatim in the
    approval screen, because "why is this address allowed?" must be answerable
    without reading code.
    """
    reasons = []
    value = (value or "").strip()
    if "@" not in value:
        return UNKNOWN, ["Not an email address"]

    local, _, domain = value.lower().partition("@")
    if role_based is None:
        role_based = local in extractor.ROLE_LOCAL_PARTS

    if provider == "spotify":
        return SPOTIFY_ONLY_SOURCE, [
            "Found only through Spotify. Spotify data may not be used to resolve "
            "an outreach contact; an independent professional source is required."
        ]
    if hidden_only:
        return PROHIBITED_PRIVATE_INFORMATION, [
            "Address appears only in hidden markup — treated as a honeypot, never contacted."
        ]
    if provider in ("data_broker", "purchased_list"):
        return DATA_BROKER_SOURCE, ["Unlicensed contact list source — not usable."]

    if freemail_domain(domain):
        if _PERSONAL_NAME_RE.match(local) or not role_based:
            return PERSONAL_ADDRESS, [
                f"Free-mail domain ({domain}) with a personal-looking mailbox — "
                "treated as a personal address, not a business route."
            ]

    if page_classification == extractor.SUBMISSION_GUIDELINES:
        reasons.append("Published on a page whose purpose is music submissions")
        if role_based:
            reasons.append(f"Role-based mailbox ({local}@)")
        return EXPLICIT_SUBMISSION_ROUTE, reasons

    if role_based:
        reasons.append(f"Role-based mailbox ({local}@)")
        if origin == "mailto-link":
            reasons.append("Published as a mailto link, not scraped from prose")
        if page_is_official or independent_domains >= 2:
            reasons.append(
                f"Corroborated by {independent_domains} independent source(s)"
                if independent_domains >= 2 else "Published on the outlet's own domain"
            )
            return VERIFIED_PUBLIC_BUSINESS, reasons
        return ROLE_BASED_ADDRESS, reasons

    if page_classification in (extractor.BUSINESS_CONTACT, extractor.CURATOR,
                               extractor.PUBLICATION, extractor.RADIO,
                               extractor.BLOG, extractor.DJ_POOL):
        reasons.append(f"Published on a {page_classification.lower().replace('_', ' ')} page")
        if independent_domains >= 2:
            reasons.append(f"Corroborated by {independent_domains} independent domains")
            return VERIFIED_PUBLIC_BUSINESS, reasons
        reasons.append("Only one source has been seen so far")
        return UNVERIFIED_PUBLIC_BUSINESS, reasons

    return UNKNOWN, ["Could not establish that this is a professional submission route"]


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def ensure_organization(name, domain, kind, official=False, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    row = db.query_one(
        "SELECT * FROM organization WHERE tenant_id = ? AND domain IS ? AND name = ?",
        (tenant_id, domain, name),
    )
    if row:
        return row["id"]
    if domain:
        row = db.query_one(
            "SELECT * FROM organization WHERE tenant_id = ? AND domain = ?",
            (tenant_id, domain),
        )
        if row:
            return row["id"]
    organization_id = db.new_id("org")
    db.insert("organization", {
        "id": organization_id,
        "tenant_id": tenant_id,
        "name": name,
        "domain": domain,
        "kind": kind,
        "official": 1 if official else 0,
        "canonical_id": None,
        "created_at": clock.now_iso(),
    })
    return organization_id


def ensure_contact(organization_id, display_name=None, role=None, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    row = db.query_one(
        "SELECT * FROM professional_contact WHERE tenant_id = ? AND organization_id IS ? "
        "AND display_name IS ?",
        (tenant_id, organization_id, display_name),
    )
    if row:
        return row["id"]
    contact_id = db.new_id("con")
    db.insert("professional_contact", {
        "id": contact_id,
        "tenant_id": tenant_id,
        "organization_id": organization_id,
        "display_name": display_name,
        "role": role,
        "canonical_id": None,
        "created_at": clock.now_iso(),
    })
    return contact_id


def record_method(contact_id, value, category, kind=EMAIL, role_based=False,
                  spotify_only=False, independent_source_count=0, verified=False,
                  evidence_id=None, tenant_id=None):
    """Store a contact method: ciphertext + keyed hash, never plaintext."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    normalized = crypto.normalize_email(value) if kind == EMAIL else value.strip()
    value_hash = crypto.hash_value(normalized) if kind == EMAIL else crypto.content_hash(normalized)
    domain = normalized.split("@")[-1] if "@" in normalized else None

    existing = db.query_one(
        "SELECT * FROM contact_method WHERE tenant_id = ? AND value_hash = ?",
        (tenant_id, value_hash),
    )
    if existing is not None:
        updates = {}
        if independent_source_count > (existing["independent_source_count"] or 0):
            updates["independent_source_count"] = independent_source_count
        # A category may only strengthen from corroboration, never weaken silently.
        if category in SENDABLE_CATEGORIES and existing["category"] == UNVERIFIED_PUBLIC_BUSINESS:
            updates["category"] = category
        if verified and not existing["verified_at"]:
            updates["verified_at"] = clock.now_iso()
        if updates:
            db.update("contact_method", existing["id"], updates)
        if evidence_id:
            _link_evidence(existing["id"], evidence_id)
        return existing["id"]

    method_id = db.new_id("cm")
    db.insert("contact_method", {
        "id": method_id,
        "tenant_id": tenant_id,
        "contact_id": contact_id,
        "kind": kind,
        "category": category,
        "value_ciphertext": crypto.encrypt(normalized),
        "value_hash": value_hash,
        "value_preview": crypto.preview_email(normalized) if kind == EMAIL else normalized[:60],
        "domain": domain,
        "role_based": 1 if role_based else 0,
        "spotify_only": 1 if spotify_only else 0,
        "independent_source_count": independent_source_count,
        "verified_at": clock.now_iso() if verified else None,
        "mx_checked_at": None,
        "mx_ok": None,
        "created_at": clock.now_iso(),
    })
    if evidence_id:
        _link_evidence(method_id, evidence_id)
    audit.record("contact.recorded", entity_type="contact_method", entity_id=method_id,
                 payload={"category": category, "domain": domain})
    return method_id


def _link_evidence(method_id, evidence_id):
    existing = db.query_one(
        "SELECT id FROM contact_evidence WHERE contact_method_id = ? AND evidence_id = ?",
        (method_id, evidence_id),
    )
    if existing:
        return existing["id"]
    link_id = db.new_id("ce")
    db.insert("contact_evidence", {
        "id": link_id,
        "contact_method_id": method_id,
        "evidence_id": evidence_id,
    })
    return link_id


def methods_for_contact(contact_id):
    return db.query(
        "SELECT * FROM contact_method WHERE contact_id = ? ORDER BY created_at", (contact_id,)
    )


def get_method(method_id):
    return db.query_one("SELECT * FROM contact_method WHERE id = ?", (method_id,))


def reveal(method_id):
    """Decrypt a contact value. Only the send path and the approval screen call
    this, and every call is audited."""
    row = get_method(method_id)
    if row is None:
        return None
    audit.record("contact.revealed", entity_type="contact_method", entity_id=method_id,
                 actor_kind=audit.ACTOR_USER)
    return crypto.decrypt(row["value_ciphertext"])


def method_evidence(method_id):
    rows = db.query(
        "SELECT e.* FROM contact_evidence ce JOIN evidence_packet e ON e.id = ce.evidence_id "
        "WHERE ce.contact_method_id = ? ORDER BY e.retrieved_at DESC",
        (method_id,),
    )
    return rows


def independent_domains(method_id):
    rows = method_evidence(method_id)
    return len({row["source_domain"] for row in rows if row["source_domain"]})


# --------------------------------------------------------------------------
# suppression
# --------------------------------------------------------------------------

GLOBAL = "GLOBAL"
TENANT = "TENANT"

OPT_OUT = "OPT_OUT"
HARD_BOUNCE = "HARD_BOUNCE"
COMPLAINT = "COMPLAINT"
MANUAL = "MANUAL"
NEGATIVE_REQUEST = "NEGATIVE_REQUEST"


def suppress(value, reason, scope=GLOBAL, source="system", tenant_id=None):
    """Add to the suppression list. Suppression is never removed by REACH."""
    value_hash = crypto.hash_value(value) if "@" in (value or "") else value
    if scope == TENANT:
        tenant_id = tenant_id or rbac.current_principal().tenant_id
    else:
        tenant_id = None
    existing = db.query_one(
        "SELECT id FROM suppression_record WHERE value_hash = ? AND scope = ? "
        "AND tenant_id IS ?",
        (value_hash, scope, tenant_id),
    )
    if existing:
        return existing["id"]
    record_id = db.new_id("sup")
    db.insert("suppression_record", {
        "id": record_id,
        "tenant_id": tenant_id,
        "value_hash": value_hash,
        "scope": scope,
        "reason": reason,
        "source": source,
        "created_at": clock.now_iso(),
    })
    audit.record("suppression.added", entity_type="suppression", entity_id=record_id,
                 payload={"reason": reason, "scope": scope})
    return record_id


def is_suppressed(value=None, value_hash=None, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    if value_hash is None:
        value_hash = crypto.hash_value(value)
    row = db.query_one(
        "SELECT * FROM suppression_record WHERE value_hash = ? "
        "AND (scope = ? OR (scope = ? AND tenant_id = ?)) LIMIT 1",
        (value_hash, GLOBAL, TENANT, tenant_id),
    )
    return row


def suppression_list(tenant_id=None, limit=200):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    return db.query(
        "SELECT * FROM suppression_record WHERE scope = ? OR tenant_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (GLOBAL, tenant_id, limit),
    )


# --------------------------------------------------------------------------
# sendability
# --------------------------------------------------------------------------

def sendability(method_id, tenant_id=None):
    """Everything that decides whether this address may receive a message.

    Returns a dict with ``sendable`` and the full list of blocking reasons —
    never a bare boolean, because the UI has to explain the refusal.
    """
    row = get_method(method_id)
    if row is None:
        return {"sendable": False, "blockers": ["Contact method not found"], "category": UNKNOWN}

    blockers = []
    notes = []

    if row["kind"] != EMAIL:
        blockers.append(f"{row['kind']} routes are not sent by REACH in Phase One")

    if row["spotify_only"]:
        blockers.append(
            "Address was found only through Spotify. Independent verification on a "
            "professional source outside Spotify is required before any email."
        )

    if row["category"] not in SENDABLE_CATEGORIES:
        blockers.append(f"Contact category {row['category']} is not eligible for direct email")
    else:
        notes.append(f"Category {row['category']} is eligible")

    suppressed = is_suppressed(value_hash=row["value_hash"], tenant_id=tenant_id)
    if suppressed is not None:
        blockers.append(f"Suppressed ({suppressed['reason']}) — permanently excluded")

    domains = independent_domains(method_id)
    if row["category"] == UNVERIFIED_PUBLIC_BUSINESS and domains < 2:
        blockers.append("Business address is not independently verified")
    if domains:
        notes.append(f"Published on {domains} independent domain(s)")

    return {
        "sendable": not blockers,
        "blockers": blockers,
        "notes": notes,
        "category": row["category"],
        "preview": row["value_preview"],
        "independent_domains": domains,
        "verified_at": row["verified_at"],
    }


# --------------------------------------------------------------------------
# domain checks
# --------------------------------------------------------------------------

def check_domain(method_id):
    """Non-intrusive validity check: does the domain have a mail exchanger?

    Uses DNS only. REACH never probes a mailbox with SMTP callbacks.
    """
    row = get_method(method_id)
    if row is None or not row["domain"]:
        return None
    ok = _has_mx(row["domain"])
    db.update("contact_method", method_id, {
        "mx_checked_at": clock.now_iso(),
        "mx_ok": 1 if ok else 0,
    })
    return ok


def _has_mx(domain):
    """True when the domain publishes a mail exchanger (or an A record, which
    RFC 5321 still allows to accept mail).

    A positive answer means "this domain can receive mail", not "this mailbox
    exists" — REACH never probes a mailbox with an SMTP callback, so the result
    is stored as a weak signal and never as proof of deliverability.
    """
    from . import dns_checks

    return dns_checks.mx(domain).found


def never_guess(local_part, domain):
    """Explicitly refuses the "firstname.lastname@company" pattern.

    Kept as a callable so the prohibition is testable rather than merely
    documented.
    """
    raise ValidationError(
        "REACH does not construct addresses from a person's name and a company "
        f"domain ({local_part}@{domain}). A published route is required."
    )


def consolidate_targets(method_id, outlet_ids):
    """One curator, many playlists, one message.

    Returns the dedup key that all of those outlets share, so the campaign
    creates a single consolidated target instead of N duplicate emails.
    """
    row = get_method(method_id)
    if row is None:
        return None
    return f"contact:{row['value_hash']}"


def record_from_extraction(extracted, source_document_id=None, provider="open_web_fetch",
                           campaign_id=None, tenant_id=None):
    """Turn one extracted page into organization + contact + methods + evidence.

    Hidden-only addresses are recorded as PROHIBITED so a honeypot is captured
    and permanently unusable rather than silently dropped.
    """
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    domain = extracted["domain"]
    organization_id = ensure_organization(
        extracted["outlet_name"], domain, extracted["classification"],
        official=True, tenant_id=tenant_id,
    )
    contact_id = ensure_contact(organization_id, tenant_id=tenant_id)

    hidden = {value.lower() for value in extracted.get("hidden_emails", [])}
    visible = {candidate["value"].lower() for candidate in extracted.get("contacts", [])}
    method_ids = []

    for candidate in extracted.get("contacts", []):
        value = candidate["value"]
        category, reasons = classify(
            value,
            page_classification=extracted["classification"],
            provider=provider,
            origin=candidate["origin"],
            role_based=candidate["role_based"],
            independent_domains=1,
            hidden_only=False,
            page_is_official=_same_domain(value, domain),
        )
        evidence_id = evidence.record(
            entity_type="contact_method",
            entity_id="pending",
            supports_field="email",
            value=crypto.preview_email(value),
            source_url=extracted["url"],
            source_domain=domain,
            source_type="OFFICIAL" if _same_domain(value, domain) else "THIRD_PARTY",
            excerpt=extracted.get("submissions_excerpt") or extracted.get("classification_excerpt"),
            confidence=0.8 if candidate["origin"] == "mailto-link" else 0.5,
            extractor_version=extracted["extractor_version"],
            source_document_id=source_document_id,
            tenant_id=tenant_id,
        )
        method_id = record_method(
            contact_id, value, category,
            role_based=candidate["role_based"],
            spotify_only=(provider == "spotify"),
            independent_source_count=1,
            verified=category in SENDABLE_CATEGORIES,
            evidence_id=evidence_id,
            tenant_id=tenant_id,
        )
        db.update("evidence_packet", evidence_id, {"entity_id": method_id})
        method_ids.append((method_id, category, reasons))

    for value in sorted(hidden - visible):
        evidence_id = evidence.record(
            entity_type="contact_method", entity_id="pending", supports_field="email",
            value=crypto.preview_email(value), source_url=extracted["url"],
            source_domain=domain, source_type="HIDDEN_MARKUP",
            excerpt="Address present only in hidden markup", confidence=0.9,
            extractor_version=extracted["extractor_version"],
            source_document_id=source_document_id, tenant_id=tenant_id,
        )
        method_id = record_method(
            contact_id, value, PROHIBITED_PRIVATE_INFORMATION,
            independent_source_count=0, evidence_id=evidence_id, tenant_id=tenant_id,
        )
        db.update("evidence_packet", evidence_id, {"entity_id": method_id})
        suppress(value, "Honeypot: address published only in hidden markup",
                 scope=GLOBAL, source="crawler")
        method_ids.append((method_id, PROHIBITED_PRIVATE_INFORMATION,
                           ["Hidden-markup honeypot"]))

    return {
        "organization_id": organization_id,
        "contact_id": contact_id,
        "methods": method_ids,
    }


def _same_domain(email_value, page_domain):
    from .netguard import registrable_domain

    if "@" not in (email_value or "") or not page_domain:
        return False
    email_domain = email_value.split("@", 1)[1].lower()
    return registrable_domain(email_domain) == registrable_domain(page_domain)
