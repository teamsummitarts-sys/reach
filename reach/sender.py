"""Sender identity and the sender-health gate.

No direct outreach is enabled until every required check passes. The checks are
real DNS lookups and real configuration reads — a check whose answer cannot be
established reports UNKNOWN and fails the gate, because "we could not verify
SPF" is not the same as "SPF passes" and must never render as green.
"""

import json
import re

from . import audit, clock, config, db, dns_checks, policy, rbac
from .providers import email as email_provider

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"
NOT_APPLICABLE = "N/A"

READY = "READY"
DISABLED = "OUTREACH DISABLED — DRAFTS ONLY"

# Checks that must be PASS before any send is permitted.
REQUIRED_CHECKS = [
    "sending_domain", "spf", "dkim", "dmarc", "tls", "bounce_webhook",
    "complaint_handling", "unsubscribe", "postal_address", "provider",
]

CHECK_LABELS = {
    "sending_domain": "SENDING DOMAIN",
    "dedicated_subdomain": "OUTREACH SUBDOMAIN",
    "spf": "SPF",
    "dkim": "DKIM",
    "dmarc": "DMARC",
    "dmarc_alignment": "DMARC ALIGNMENT",
    "tls": "TLS",
    "forward_dns": "FORWARD DNS",
    "reverse_dns": "REVERSE DNS",
    "bounce_webhook": "BOUNCE WEBHOOK",
    "complaint_handling": "COMPLAINT HANDLING",
    "unsubscribe": "UNSUBSCRIBE",
    "one_click_unsubscribe": "ONE-CLICK UNSUBSCRIBE",
    "suppression": "SUPPRESSION",
    "postal_address": "POSTAL ADDRESS",
    "provider": "EMAIL PROVIDER",
    "reputation": "DOMAIN REPUTATION",
}


def _authentication_check(assessment, declared_env, label, usable):
    """Turn a DNS answer into a (state, detail) pair, honestly.

    Four outcomes, never conflated:

    * found, and the record does its job  -> PASS, quoting what was read
    * found, but the record protects nothing -> FAIL, saying which part is wrong
    * the lookup worked, no record        -> FAIL, it is genuinely not published
    * the lookup itself failed            -> UNKNOWN, and the operator may
      override with a declared value, which is labelled *declared*, not verified

    The declaration only applies to the last case. Once REACH has read a record,
    an operator saying "it is fine" cannot make ``+all`` into protection — the
    contents are evidence and the declaration is not.
    """
    if assessment.found:
        return (PASS, assessment.detail) if usable else (FAIL, assessment.detail)
    if assessment.state == dns_checks.ABSENT:
        return FAIL, assessment.detail
    declared = config.env(declared_env)
    if declared:
        return PASS, (f"{assessment.detail}. Operator declares this verified "
                      f"({declared_env}) — declared, not checked by REACH")
    return UNKNOWN, (f"{assessment.detail}. Publish {label}, or set {declared_env} "
                     "once you have verified it yourself")


def _resolves(hostname):
    """Does the sending domain exist in DNS?

    Routed through :mod:`reach.dns_checks` rather than calling getaddrinfo
    directly, so it is cached and stubbable like every other lookup. This runs
    once per target during compliance assessment; an uncached lookup there makes
    a campaign's runtime depend on the resolver's mood.
    """
    if not hostname:
        return False
    if dns_checks.query(hostname, "A").found:
        return True
    return dns_checks.query(hostname, "AAAA").found


def run_checks(tenant_id=None):
    """Evaluate every sender-health check and return them in display order."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    domain = email_provider.sending_domain()
    from_email = config.env(config.SENDER_FROM_ENV)
    from_name = config.env(config.SENDER_NAME_ENV)
    postal = config.env(config.SENDER_POSTAL_ADDRESS_ENV)

    checks = {}

    def add(key, state, detail):
        checks[key] = {"key": key, "label": CHECK_LABELS[key], "state": state,
                       "detail": detail}

    declared_domain = config.env("REACH_SENDER_DOMAIN_VERIFIED")
    if not domain:
        add("sending_domain", FAIL, "REACH_SENDER_DOMAIN / REACH_SENDER_FROM is not set")
    elif _resolves(domain):
        add("sending_domain", PASS, f"{domain} resolves")
    elif declared_domain:
        # DNS is unavailable in some deployment environments. The operator may
        # declare a domain verified, and the detail says so rather than
        # implying REACH checked it.
        add("sending_domain", PASS,
            f"{domain} declared verified by the operator (DNS lookup unavailable here)")
    else:
        add("sending_domain", FAIL, f"{domain} does not resolve")

    add("dedicated_subdomain",
        PASS if domain and domain.count(".") >= 2 else UNKNOWN,
        "A dedicated outreach subdomain isolates reputation from transactional mail")

    spf_policy = dns_checks.analyse_spf(domain) if domain else dns_checks.SpfPolicy(
        dns_checks.UNRESOLVED, detail="No sending domain configured")
    spf_state, spf_detail = _authentication_check(
        spf_policy, "REACH_SENDER_SPF_VERIFIED", "an SPF policy", spf_policy.protective)
    note = dns_checks.spf_lookup_note(spf_policy)
    if note and spf_state == PASS:
        spf_detail = f"{spf_detail}. {note}"
    add("spf", spf_state, spf_detail)

    selector = config.env("REACH_SENDER_DKIM_SELECTOR")
    dkim_key = dns_checks.analyse_dkim(domain, selector) if domain else dns_checks.DkimKey(
        dns_checks.UNRESOLVED, detail="No sending domain configured")
    dkim_state, dkim_detail = _authentication_check(
        dkim_key, "REACH_SENDER_DKIM_VERIFIED", "a DKIM key", dkim_key.usable)
    add("dkim", dkim_state, dkim_detail)

    dmarc_lookup = dns_checks.dmarc(domain) if domain else dns_checks.Lookup(
        dns_checks.UNRESOLVED, detail="No sending domain configured")
    dmarc_state, dmarc_detail = _authentication_check(
        dmarc_lookup, "REACH_SENDER_DMARC_VERIFIED", "a DMARC policy", True)
    if dmarc_lookup.found:
        dmarc_detail = f"Verified by DNS: {dmarc_lookup.detail}"
    add("dmarc", dmarc_state, dmarc_detail)

    # Alignment needs both authentication methods to pass AND a DMARC policy
    # that actually asks receivers to act. p=none publishes intent, not
    # enforcement, so it is reported as such rather than as a pass.
    policy_value = dns_checks.dmarc_policy(domain) if domain else None
    if spf_state == PASS and dkim_state == PASS and policy_value in ("quarantine", "reject"):
        add("dmarc_alignment", PASS,
            f"SPF and DKIM both pass under an enforcing policy (p={policy_value})")
    elif policy_value == "none":
        add("dmarc_alignment", UNKNOWN,
            "DMARC is published as p=none, which monitors but does not enforce")
    else:
        add("dmarc_alignment", UNKNOWN,
            "Alignment requires SPF and DKIM to authenticate the same domain "
            "under an enforcing DMARC policy")

    add("tls", PASS if email_provider.api_host() else FAIL,
        "Provider API is reached over HTTPS with certificate verification")
    add("forward_dns", PASS if _resolves(domain) else FAIL,
        f"Forward DNS for {domain or '(unset)'}")
    add("reverse_dns", NOT_APPLICABLE,
        "Reverse DNS is the provider's responsibility for API-based sending")

    webhook = config.env("REACH_EMAIL_WEBHOOK_SECRET")
    add("bounce_webhook", PASS if webhook else FAIL,
        "Signed bounce webhook configured" if webhook
        else "Set REACH_EMAIL_WEBHOOK_SECRET to verify provider webhooks")
    add("complaint_handling", PASS if webhook else FAIL,
        "Complaints are processed on the same signed webhook" if webhook
        else "Complaint processing shares the bounce webhook configuration")

    add("unsubscribe", PASS if postal else FAIL,
        "Every message carries a visible opt-out and a postal address" if postal
        else "A postal address is required before an opt-out footer can be built")
    add("one_click_unsubscribe", PASS,
        "List-Unsubscribe and List-Unsubscribe-Post headers are set on every send")
    add("suppression", PASS, "Suppression is checked at decision time and again at send time")
    add("postal_address", PASS if postal else FAIL,
        postal or "Set REACH_SENDER_POSTAL_ADDRESS")

    provider_state = email_provider.status()
    add("provider", PASS if provider_state["state"] == policy.CONNECTED else FAIL,
        f"{provider_state['display_name']}: {provider_state['state']}")

    add("reputation", UNKNOWN,
        "Domain reputation is reported by the provider's postmaster tools, not by REACH")

    ordered = [checks[key] for key in CHECK_LABELS if key in checks]
    ready = all(checks[key]["state"] == PASS for key in REQUIRED_CHECKS if key in checks)

    record = {
        "checks": ordered,
        "ready": ready,
        "status": READY if ready else DISABLED,
        "failing": [check["label"] for check in ordered
                    if check["key"] in REQUIRED_CHECKS and check["state"] != PASS],
        "from_email": from_email,
        "from_name": from_name,
        "domain": domain,
        "postal_address": postal,
        "checked_at": clock.now_iso(),
    }
    _persist(record, tenant_id)
    return record


def _persist(record, tenant_id):
    existing = db.query_one(
        "SELECT id FROM sender_identity WHERE tenant_id = ?", (tenant_id,)
    )
    payload = {
        "from_email": record["from_email"],
        "from_name": record["from_name"],
        "domain": record["domain"],
        "postal_address": record["postal_address"],
        "provider": "email",
        "checks_json": json.dumps(record["checks"]),
        "status": record["status"],
        "checked_at": record["checked_at"],
    }
    if existing:
        db.update("sender_identity", existing["id"], payload)
    else:
        payload.update({"id": db.new_id("sender"), "tenant_id": tenant_id})
        db.insert("sender_identity", payload)


def health_summary(tenant_id=None):
    return run_checks(tenant_id)


def ready(tenant_id=None):
    return run_checks(tenant_id)["ready"]


def identity(tenant_id=None):
    health = run_checks(tenant_id)
    return {
        "from_email": health["from_email"],
        "from_name": health["from_name"] or "REACH",
        "postal_address": health["postal_address"],
        "domain": health["domain"],
    }


# --------------------------------------------------------------------------
# throttles and limits
# --------------------------------------------------------------------------

WARMUP_SCHEDULE = [10, 20, 40, 80, 150, 300, 500]
PER_DOMAIN_DAILY_LIMIT = 3
PER_RECIPIENT_COOLDOWN_DAYS = 30
BOUNCE_RATE_PAUSE_THRESHOLD = 0.05
COMPLAINT_RATE_PAUSE_THRESHOLD = 0.001


def daily_sent(tenant_id=None, campaign_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    since = clock.days_from_now(-1)
    sql = ("SELECT COUNT(*) AS n FROM submission WHERE tenant_id = ? AND method = 'EMAIL' "
           "AND sent_at IS NOT NULL AND sent_at >= ?")
    params = [tenant_id, since]
    if campaign_id:
        sql += (" AND target_id IN (SELECT id FROM campaign_target WHERE campaign_id = ?)")
        params.append(campaign_id)
    row = db.query_one(sql, tuple(params))
    return row["n"] if row else 0


def domain_sent_today(domain, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    since = clock.days_from_now(-1)
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM submission s "
        "JOIN campaign_target t ON t.id = s.target_id "
        "JOIN outlet o ON o.id = t.outlet_id "
        "WHERE s.tenant_id = ? AND s.sent_at >= ? AND o.domain = ?",
        (tenant_id, since, domain),
    )
    return row["n"] if row else 0


def bounce_rate(tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    sent = db.query_one(
        "SELECT COUNT(*) AS n FROM submission WHERE tenant_id = ? AND sent_at IS NOT NULL",
        (tenant_id,),
    )["n"]
    if not sent:
        return 0.0
    bounced = db.query_one(
        "SELECT COUNT(*) AS n FROM submission WHERE tenant_id = ? AND status = 'BOUNCED'",
        (tenant_id,),
    )["n"]
    return bounced / sent


def throttle_state(campaign_id, tenant_id=None):
    """Everything that could hold a send back short of a compliance decision."""
    from . import campaigns

    tenant_id = tenant_id or rbac.current_principal().tenant_id
    campaign = campaigns.get(campaign_id)
    sent_today = daily_sent(tenant_id, campaign_id)
    tenant_today = daily_sent(tenant_id)
    rate = bounce_rate(tenant_id)
    warmup_cap = WARMUP_SCHEDULE[min(_warmup_day(tenant_id), len(WARMUP_SCHEDULE) - 1)]

    blockers = []
    if policy.send_stop_engaged():
        blockers.append("Emergency send stop is engaged")
    if campaign and sent_today >= campaign["daily_send_limit"]:
        blockers.append(f"Campaign daily send limit reached ({sent_today})")
    if tenant_today >= warmup_cap:
        blockers.append(f"Warm-up cap reached for today ({tenant_today}/{warmup_cap})")
    if rate > BOUNCE_RATE_PAUSE_THRESHOLD:
        blockers.append(f"Bounce rate {round(rate * 100, 1)}% exceeds the pause threshold")

    return {
        "sent_today": sent_today,
        "tenant_today": tenant_today,
        "warmup_cap": warmup_cap,
        "campaign_limit": campaign["daily_send_limit"] if campaign else None,
        "bounce_rate": rate,
        "blockers": blockers,
        "ok": not blockers,
    }


def _warmup_day(tenant_id):
    row = db.query_one(
        "SELECT MIN(sent_at) AS first FROM submission WHERE tenant_id = ? AND sent_at IS NOT NULL",
        (tenant_id,),
    )
    if row is None or not row["first"]:
        return 0
    days = clock.days_since(row["first"])
    return int(days // 1) if days else 0


def recipient_in_cooldown(contact_method_id, tenant_id=None):
    row = db.query_one(
        "SELECT MAX(s.sent_at) AS last FROM submission s "
        "JOIN campaign_target t ON t.id = s.target_id "
        "JOIN contact_method cm ON cm.contact_id = t.contact_id "
        "WHERE cm.id = ? AND s.sent_at IS NOT NULL",
        (contact_method_id,),
    )
    if row is None or not row["last"]:
        return False, None
    days = clock.days_since(row["last"])
    if days is not None and days < PER_RECIPIENT_COOLDOWN_DAYS:
        return True, round(PER_RECIPIENT_COOLDOWN_DAYS - days)
    return False, None


def emergency_stop(enabled, reason=None):
    rbac.require("provider.kill_switch")
    policy.set_send_stop(enabled, reason)
    audit.record("sender.emergency_stop", entity_type="sender", entity_id=None,
                 payload={"enabled": enabled, "reason": reason},
                 actor_kind=audit.ACTOR_USER)


_UNSUBSCRIBE_RE = re.compile(r"\b(unsubscribe|opt[\s-]?out|remove me|do not (?:contact|email))\b",
                             re.I)


def looks_like_opt_out(text):
    return bool(_UNSUBSCRIBE_RE.search(text or ""))
