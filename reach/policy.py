"""Provider capability and policy registry.

Every external provider has exactly one machine-readable policy record here.
Provider restrictions are never scattered through calling code: adapters ask
this module, and this module raises.

The baseline below is a snapshot taken while building Phase One. It is not
permanent truth — each record carries ``last_verified_at``/``next_review_at``
and the Provider Health screen surfaces reviews that have come due.
"""

import json
from dataclasses import asdict, dataclass, field

from . import clock, db
from .errors import KillSwitchEngaged, PolicyViolation, ProviderNotConnected, QuotaExceeded

# --- status ----------------------------------------------------------------

ACTIVE = "ACTIVE"
LIMITED = "LIMITED"
MANUAL_ONLY = "MANUAL_ONLY"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
BETA = "BETA"
DISABLED = "DISABLED"
BLOCKED = "BLOCKED"

PROVIDER_STATUSES = [ACTIVE, LIMITED, MANUAL_ONLY, APPROVAL_REQUIRED, BETA, DISABLED, BLOCKED]

# --- capabilities ----------------------------------------------------------

CATALOG_SEARCH = "CATALOG_SEARCH"
PLAYLIST_DISCOVERY = "PLAYLIST_DISCOVERY"
CURATOR_DISCOVERY = "CURATOR_DISCOVERY"
PUBLIC_PROFILE_DISCOVERY = "PUBLIC_PROFILE_DISCOVERY"
AUTHENTICATED_READ = "AUTHENTICATED_READ"
AUTHENTICATED_WRITE = "AUTHENTICATED_WRITE"
EDITORIAL_PITCH = "EDITORIAL_PITCH"
PUBLIC_CONTACT_RESOLUTION = "PUBLIC_CONTACT_RESOLUTION"
SUBMISSION_FORM = "SUBMISSION_FORM"
PLACEMENT_MONITORING = "PLACEMENT_MONITORING"
ANALYTICS_IMPORT = "ANALYTICS_IMPORT"

PROVIDER_CAPABILITIES = [
    CATALOG_SEARCH, PLAYLIST_DISCOVERY, CURATOR_DISCOVERY, PUBLIC_PROFILE_DISCOVERY,
    AUTHENTICATED_READ, AUTHENTICATED_WRITE, EDITORIAL_PITCH, PUBLIC_CONTACT_RESOLUTION,
    SUBMISSION_FORM, PLACEMENT_MONITORING, ANALYTICS_IMPORT,
]

POLICY_REGISTRY_VERSION = "2026-08-16"


@dataclass
class ProviderPolicy:
    provider: str
    status: str
    capabilities: list = field(default_factory=list)
    officialApiRequired: bool = True
    webDiscoveryAllowed: bool = False
    authenticatedBrowserAutomationAllowed: bool = False
    commercialApprovalRequired: bool = False
    humanActionRequired: bool = False
    mayStoreRawData: bool = False
    mayCreateEmbeddings: bool = False
    maySendDataToLanguageModel: bool = False
    mayUseForModelTraining: bool = False
    mayResolveContactsFromProviderData: bool = False
    retentionDays: int = None
    attributionRequired: bool = False
    disconnectDeletionRequired: bool = False
    rateLimitStrategy: str = "exponential-backoff"
    policySourceName: str = ""
    policyVersion: str = POLICY_REGISTRY_VERSION
    effectiveAt: str = "2026-08-16T00:00:00+00:00"
    lastVerifiedAt: str = "2026-08-16T00:00:00+00:00"
    nextReviewAt: str = "2026-11-16T00:00:00+00:00"
    killSwitchEnabled: bool = False
    displayName: str = ""
    notes: list = field(default_factory=list)

    def to_json(self):
        return json.dumps(asdict(self), sort_keys=True)

    def allows(self, capability):
        return capability in self.capabilities


def _p(**kwargs):
    return ProviderPolicy(**kwargs)


# --------------------------------------------------------------------------
# baseline registry
# --------------------------------------------------------------------------

BASELINE_POLICIES = [
    _p(
        provider="reach_catalog",
        displayName="REACH Catalog",
        status=ACTIVE,
        capabilities=[CATALOG_SEARCH, ANALYTICS_IMPORT],
        officialApiRequired=False,
        mayStoreRawData=True,
        mayCreateEmbeddings=True,
        maySendDataToLanguageModel=True,
        mayUseForModelTraining=False,
        mayResolveContactsFromProviderData=False,
        rateLimitStrategy="in-process",
        policySourceName="Internal — first-party data owned by the tenant",
        notes=["User-owned catalog data. Model use still requires a rights attestation."],
    ),
    _p(
        provider="open_web_search",
        displayName="Open Web Search",
        status=LIMITED,
        capabilities=[PLAYLIST_DISCOVERY, CURATOR_DISCOVERY, PUBLIC_PROFILE_DISCOVERY],
        webDiscoveryAllowed=True,
        mayStoreRawData=False,
        mayCreateEmbeddings=True,
        maySendDataToLanguageModel=True,
        mayResolveContactsFromProviderData=True,
        retentionDays=180,
        rateLimitStrategy="token-bucket + provider quota",
        policySourceName="Search provider API terms (Brave Search API / Google Programmable Search)",
        notes=[
            "Search result snippets only. The fetcher, not the search provider, retrieves pages.",
            "Requires REACH_SEARCH_API_KEY. Without it the adapter reports NOT CONNECTED.",
        ],
    ),
    _p(
        provider="open_web_fetch",
        displayName="Open Web Fetch",
        status=LIMITED,
        capabilities=[PUBLIC_PROFILE_DISCOVERY, PUBLIC_CONTACT_RESOLUTION, SUBMISSION_FORM],
        officialApiRequired=False,
        webDiscoveryAllowed=True,
        mayStoreRawData=False,
        mayCreateEmbeddings=False,
        maySendDataToLanguageModel=True,
        mayResolveContactsFromProviderData=True,
        retentionDays=180,
        attributionRequired=True,
        rateLimitStrategy="per-domain budget + robots crawl-delay",
        policySourceName="robots.txt directives and per-site terms",
        notes=[
            "Publicly accessible pages only. Never behind a login or paywall.",
            "Excerpts and hashes are stored; full pages are not retained.",
        ],
    ),
    _p(
        provider="youtube",
        displayName="YouTube Data API",
        status=LIMITED,
        capabilities=[CATALOG_SEARCH, PLAYLIST_DISCOVERY, CURATOR_DISCOVERY, PUBLIC_PROFILE_DISCOVERY, PLACEMENT_MONITORING],
        webDiscoveryAllowed=False,
        mayStoreRawData=False,
        mayCreateEmbeddings=False,
        maySendDataToLanguageModel=False,
        mayResolveContactsFromProviderData=False,
        retentionDays=30,
        attributionRequired=True,
        disconnectDeletionRequired=True,
        rateLimitStrategy="daily quota units + exponential backoff",
        policySourceName="YouTube API Services Terms of Service and Developer Policies",
        notes=[
            "Quota is accounted per request in units; search costs far more than list.",
            "The API does not expose private creator contact details. Business routes must "
            "be resolved from independently published professional pages.",
            "Stored API data is refreshed or deleted within 30 days.",
        ],
    ),
    _p(
        provider="soundcloud",
        displayName="SoundCloud API",
        status=LIMITED,
        capabilities=[CATALOG_SEARCH, PLAYLIST_DISCOVERY, CURATOR_DISCOVERY, PUBLIC_PROFILE_DISCOVERY],
        mayStoreRawData=False,
        mayCreateEmbeddings=False,
        maySendDataToLanguageModel=False,
        mayResolveContactsFromProviderData=False,
        retentionDays=30,
        attributionRequired=True,
        disconnectDeletionRequired=True,
        rateLimitStrategy="OAuth client credentials + backoff",
        policySourceName="SoundCloud API Terms of Use",
        notes=[
            "Read-oriented discovery only in Phase One. No account actions.",
            "API application approval is required before production credentials are issued.",
        ],
    ),
    _p(
        provider="mixcloud",
        displayName="Mixcloud API",
        status=LIMITED,
        capabilities=[CATALOG_SEARCH, CURATOR_DISCOVERY, PUBLIC_PROFILE_DISCOVERY],
        mayStoreRawData=False,
        mayCreateEmbeddings=False,
        maySendDataToLanguageModel=False,
        mayResolveContactsFromProviderData=False,
        retentionDays=30,
        attributionRequired=True,
        rateLimitStrategy="backoff on 429",
        policySourceName="Mixcloud API terms",
        notes=["Show, DJ, channel and tag discovery within current API capability."],
    ),
    _p(
        provider="musicbrainz",
        displayName="MusicBrainz",
        status=ACTIVE,
        capabilities=[CATALOG_SEARCH],
        mayStoreRawData=True,
        mayCreateEmbeddings=True,
        maySendDataToLanguageModel=True,
        mayUseForModelTraining=False,
        mayResolveContactsFromProviderData=False,
        attributionRequired=True,
        rateLimitStrategy="1 request/second, identifying User-Agent required",
        policySourceName="MusicBrainz API rate-limiting and licensing guidance",
        notes=[
            "Canonical identity and metadata only.",
            "An open metadata identifier is not proof of rights ownership.",
        ],
    ),
    _p(
        provider="listenbrainz",
        displayName="ListenBrainz",
        status=LIMITED,
        capabilities=[CATALOG_SEARCH, ANALYTICS_IMPORT],
        mayStoreRawData=True,
        mayCreateEmbeddings=False,
        maySendDataToLanguageModel=False,
        mayResolveContactsFromProviderData=False,
        retentionDays=90,
        attributionRequired=True,
        rateLimitStrategy="documented rate limit headers",
        policySourceName="ListenBrainz API documentation and privacy terms",
        notes=[
            "Public or user-authorized endpoints only.",
            "Listener data is treated as personal data, not as public metadata.",
        ],
    ),
    _p(
        provider="spotify",
        displayName="Spotify Web API",
        status=LIMITED,
        capabilities=[CATALOG_SEARCH, AUTHENTICATED_READ, EDITORIAL_PITCH, PLACEMENT_MONITORING],
        webDiscoveryAllowed=False,
        authenticatedBrowserAutomationAllowed=False,
        humanActionRequired=True,
        mayStoreRawData=False,
        mayCreateEmbeddings=False,
        maySendDataToLanguageModel=False,
        mayUseForModelTraining=False,
        mayResolveContactsFromProviderData=False,
        retentionDays=30,
        attributionRequired=True,
        disconnectDeletionRequired=True,
        rateLimitStrategy="respect Retry-After; rolling window",
        policySourceName="Spotify Developer Terms and Design Guidelines",
        notes=[
            "HARD RULE: no Spotify content may enter a language model, an embedding, or "
            "model training. Enforced in reach/firewall.py, not in a prompt.",
            "HARD RULE: an address found only through Spotify is never an outreach route.",
            "No crawling or scraping of Spotify web or app surfaces.",
            "Editorial pitching happens in Spotify for Artists as a human action.",
            "Development Mode vs Extended Quota state must be displayed accurately.",
        ],
    ),
    _p(
        provider="apple_music",
        displayName="Apple Music / MusicKit",
        status=MANUAL_ONLY,
        capabilities=[CATALOG_SEARCH, EDITORIAL_PITCH],
        humanActionRequired=True,
        mayStoreRawData=False,
        maySendDataToLanguageModel=False,
        commercialApprovalRequired=True,
        attributionRequired=True,
        retentionDays=30,
        policySourceName="Apple Media Services / MusicKit terms",
        notes=[
            "No automated editorial playlist pitching exists. Distributor and label "
            "relationship routes are modelled as human actions.",
            "Never scrape Apple's private or authenticated interfaces.",
        ],
    ),
    _p(
        provider="amazon_music",
        displayName="Amazon Music for Artists",
        status=MANUAL_ONLY,
        capabilities=[EDITORIAL_PITCH],
        humanActionRequired=True,
        commercialApprovalRequired=True,
        maySendDataToLanguageModel=False,
        policySourceName="Amazon Music for Artists pitch workflow",
        notes=[
            "New-release pitch is a structured human action with a copy-ready field set.",
            "Broader Amazon Music Web API access is not claimed without granted approval.",
        ],
    ),
    _p(
        provider="pandora",
        displayName="Pandora AMP",
        status=MANUAL_ONLY,
        capabilities=[EDITORIAL_PITCH, SUBMISSION_FORM],
        humanActionRequired=True,
        maySendDataToLanguageModel=False,
        policySourceName="Pandora AMP submission requirements",
        notes=[
            "Eligibility is validated before the task is created: delivery, U.S. rights, "
            "claimed artist account, required assets.",
        ],
    ),
    _p(
        provider="audiomack",
        displayName="Audiomack",
        status=MANUAL_ONLY,
        capabilities=[EDITORIAL_PITCH, SUBMISSION_FORM],
        humanActionRequired=True,
        maySendDataToLanguageModel=False,
        policySourceName="Audiomack creator and Trending consideration terms",
        notes=["Authenticated creator workflow. No automated login or submission."],
    ),
    _p(
        provider="tidal",
        displayName="TIDAL",
        status=DISABLED,
        capabilities=[CATALOG_SEARCH],
        commercialApprovalRequired=True,
        maySendDataToLanguageModel=False,
        policySourceName="TIDAL developer terms",
        notes=[
            "Adapter exists behind a feature flag and is exercised with fixtures only.",
            "Public documentation is not evidence of granted API rights.",
        ],
    ),
    _p(
        provider="deezer",
        displayName="Deezer",
        status=DISABLED,
        capabilities=[CATALOG_SEARCH],
        commercialApprovalRequired=True,
        maySendDataToLanguageModel=False,
        policySourceName="Deezer API terms — commercial use requires permission",
        notes=["Production access stays off until commercial permission exists."],
    ),
    _p(
        provider="bandcamp",
        displayName="Bandcamp",
        status=MANUAL_ONLY,
        capabilities=[PUBLIC_PROFILE_DISCOVERY],
        officialApiRequired=False,
        webDiscoveryAllowed=True,
        humanActionRequired=True,
        maySendDataToLanguageModel=True,
        mayResolveContactsFromProviderData=True,
        retentionDays=180,
        policySourceName="Bandcamp site terms — no broad public playlist API",
        notes=["Treated as open-web discovery plus human action, not as a playlist API."],
    ),
    _p(
        provider="tiktok",
        displayName="TikTok",
        status=APPROVAL_REQUIRED,
        capabilities=[PUBLIC_PROFILE_DISCOVERY],
        commercialApprovalRequired=True,
        humanActionRequired=True,
        maySendDataToLanguageModel=False,
        mayResolveContactsFromProviderData=False,
        policySourceName="TikTok for Developers / Creator Marketplace terms",
        notes=[
            "Standard user-authorized APIs are not a creator-contact database.",
            "No automated direct messages in Phase One.",
        ],
    ),
    _p(
        provider="instagram",
        displayName="Instagram / Meta",
        status=APPROVAL_REQUIRED,
        capabilities=[PUBLIC_PROFILE_DISCOVERY],
        commercialApprovalRequired=True,
        humanActionRequired=True,
        maySendDataToLanguageModel=False,
        mayResolveContactsFromProviderData=False,
        policySourceName="Meta Platform Terms; app review required",
        notes=[
            "Business/professional-account products only, after app review.",
            "No scraping of personal accounts. No automated direct messages.",
        ],
    ),
    _p(
        provider="email",
        displayName="Transactional Email Provider",
        status=LIMITED,
        capabilities=[AUTHENTICATED_WRITE],
        mayStoreRawData=True,
        maySendDataToLanguageModel=False,
        disconnectDeletionRequired=True,
        rateLimitStrategy="per-domain throttle + warm-up schedule",
        policySourceName="Provider acceptable-use policy (anti-spam)",
        notes=[
            "Sending is gated on the sender-health check, not merely on credentials.",
            "Requires REACH_EMAIL_API_KEY. Without it REACH is drafts-only.",
        ],
    ),
    _p(
        provider="language_model",
        displayName="Language Model",
        status=DISABLED,
        capabilities=[],
        maySendDataToLanguageModel=False,
        mayUseForModelTraining=False,
        policySourceName="Internal AI use policy",
        notes=[
            "No LLM credential is configured, so pitch generation is deterministic "
            "composition over user-owned facts and permitted evidence.",
            "The firewall applies to every field regardless of which model is used.",
        ],
    ),
]

# Adapter interfaces that exist so future integration is a configuration change
# rather than a rewrite. All are off, and all require a licensed arrangement.
FUTURE_PROVIDERS = [
    ("chartmetric", "Chartmetric", "Licensed analytics API"),
    ("soundcharts", "Soundcharts", "Licensed analytics API"),
    ("viberate", "Viberate", "Licensed analytics API"),
    ("songstats", "Songstats", "Licensed analytics API"),
    ("submithub", "SubmitHub", "Paid consideration platform"),
    ("groover", "Groover", "Paid consideration platform"),
    ("playlist_push", "Playlist Push", "Paid consideration platform"),
    ("soundcampaign", "SoundCampaign", "Paid consideration platform"),
    ("dj_pool", "DJ Pools", "Licensed or contractual arrangement"),
    ("college_radio_db", "College Radio Database", "Licensed database"),
    ("sync_db", "Sync Database", "Licensed database"),
]

for _pid, _name, _kind in FUTURE_PROVIDERS:
    BASELINE_POLICIES.append(_p(
        provider=_pid,
        displayName=_name,
        status=APPROVAL_REQUIRED,
        capabilities=[],
        commercialApprovalRequired=True,
        humanActionRequired=True,
        maySendDataToLanguageModel=False,
        mayResolveContactsFromProviderData=False,
        policySourceName=f"{_name} commercial terms — {_kind}",
        notes=[
            "Adapter interface and policy record only. No data flows until a licensed "
            "API, authorized export or contractual arrangement is in place.",
            "Crawling this provider's proprietary database is prohibited.",
        ],
    ))

BASELINE_BY_ID = {p.provider: p for p in BASELINE_POLICIES}


# --------------------------------------------------------------------------
# persistence + lookup
# --------------------------------------------------------------------------

def seed_policies(force=False):
    """Write baseline records. Existing rows are left alone unless ``force``."""
    now = clock.now_iso()
    for policy in BASELINE_POLICIES:
        existing = db.query_one(
            "SELECT provider FROM provider_policy WHERE provider = ?", (policy.provider,)
        )
        if existing and not force:
            continue
        if existing:
            db.execute(
                "UPDATE provider_policy SET policy_json = ?, policy_version = ?, "
                "last_verified_at = ?, next_review_at = ?, updated_at = ? WHERE provider = ?",
                (policy.to_json(), policy.policyVersion, policy.lastVerifiedAt,
                 policy.nextReviewAt, now, policy.provider),
            )
        else:
            db.insert("provider_policy", {
                "provider": policy.provider,
                "policy_json": policy.to_json(),
                "policy_version": policy.policyVersion,
                "last_verified_at": policy.lastVerifiedAt,
                "next_review_at": policy.nextReviewAt,
                "kill_switch_enabled": 0,
                "updated_at": now,
            })


def get(provider_id):
    """Stored policy if present, otherwise the baseline. Unknown => BLOCKED."""
    row = db.query_one(
        "SELECT policy_json, kill_switch_enabled FROM provider_policy WHERE provider = ?",
        (provider_id,),
    )
    if row is not None:
        data = json.loads(row["policy_json"])
        policy = ProviderPolicy(**data)
        policy.killSwitchEnabled = bool(row["kill_switch_enabled"])
        return policy
    baseline = BASELINE_BY_ID.get(provider_id)
    if baseline is not None:
        return baseline
    return _p(
        provider=provider_id,
        displayName=provider_id,
        status=BLOCKED,
        policySourceName="Unknown provider — blocked by default",
        notes=["No policy record exists. REACH refuses to call unregistered providers."],
    )


def all_policies():
    seed_policies()
    return [get(p.provider) for p in BASELINE_POLICIES]


def set_kill_switch(provider_id, enabled, reason=None, actor=None):
    from . import audit, rbac

    rbac.require("provider.kill_switch")
    seed_policies()
    db.execute(
        "UPDATE provider_policy SET kill_switch_enabled = ?, updated_at = ? WHERE provider = ?",
        (1 if enabled else 0, clock.now_iso(), provider_id),
    )
    audit.record(
        "provider.kill_switch",
        entity_type="provider",
        entity_id=provider_id,
        payload={"enabled": bool(enabled), "reason": reason},
        actor_kind=audit.ACTOR_USER,
        actor_id=actor,
    )
    return get(provider_id)


def mark_reviewed(provider_id, next_review_days=90):
    from . import audit, rbac

    rbac.require("provider.policy_edit")
    seed_policies()
    now = clock.now_iso()
    db.execute(
        "UPDATE provider_policy SET last_verified_at = ?, next_review_at = ?, updated_at = ? "
        "WHERE provider = ?",
        (now, clock.days_from_now(next_review_days), now, provider_id),
    )
    policy = get(provider_id)
    policy.lastVerifiedAt = now
    db.execute(
        "UPDATE provider_policy SET policy_json = ? WHERE provider = ?",
        (policy.to_json(), provider_id),
    )
    audit.record("provider.policy_reviewed", entity_type="provider", entity_id=provider_id,
                 actor_kind=audit.ACTOR_USER)
    return get(provider_id)


def review_due(provider_id):
    policy = get(provider_id)
    return clock.is_past(policy.nextReviewAt)


# --------------------------------------------------------------------------
# enforcement
# --------------------------------------------------------------------------

CALLABLE_STATUSES = {ACTIVE, LIMITED, BETA}


def require_capability(provider_id, capability):
    """Raise unless this provider may perform this capability right now."""
    policy = get(provider_id)
    if policy.killSwitchEnabled:
        raise KillSwitchEngaged(f"{provider_id}: kill switch is engaged")
    if global_stop_engaged():
        raise KillSwitchEngaged("Global emergency stop is engaged")
    if policy.status in (BLOCKED, DISABLED):
        raise PolicyViolation(f"{provider_id} is {policy.status}: {policy.policySourceName}")
    if policy.status == MANUAL_ONLY:
        raise PolicyViolation(
            f"{provider_id} is MANUAL_ONLY — this action must become a human task"
        )
    if policy.status == APPROVAL_REQUIRED:
        raise PolicyViolation(
            f"{provider_id} requires commercial approval before any data flows"
        )
    if capability not in policy.capabilities:
        raise PolicyViolation(f"{provider_id} has no {capability} capability")
    return policy


def require_connected(provider_id):
    from . import config

    policy = require_capability_free(provider_id)
    if not config.credentials_present(provider_id):
        raise ProviderNotConnected(
            f"{policy.displayName or provider_id} is NOT CONNECTED: missing "
            + ", ".join(config.missing_credentials(provider_id))
        )
    return policy


def require_capability_free(provider_id):
    policy = get(provider_id)
    if policy.killSwitchEnabled:
        raise KillSwitchEngaged(f"{provider_id}: kill switch is engaged")
    if global_stop_engaged():
        raise KillSwitchEngaged("Global emergency stop is engaged")
    return policy


# --------------------------------------------------------------------------
# global emergency stop
# --------------------------------------------------------------------------

GLOBAL_STOP_KEY = "global_emergency_stop"
SEND_STOP_KEY = "outreach_send_stop"


def _switch(key):
    row = db.query_one("SELECT enabled FROM kill_switch WHERE key = ?", (key,))
    return bool(row["enabled"]) if row else False


def _set_switch(key, enabled, reason, actor):
    from . import audit

    now = clock.now_iso()
    existing = db.query_one("SELECT key FROM kill_switch WHERE key = ?", (key,))
    if existing:
        db.execute(
            "UPDATE kill_switch SET enabled = ?, reason = ?, updated_at = ?, updated_by = ? "
            "WHERE key = ?",
            (1 if enabled else 0, reason, now, actor, key),
        )
    else:
        db.insert("kill_switch", {
            "key": key, "enabled": 1 if enabled else 0, "reason": reason,
            "updated_at": now, "updated_by": actor,
        })
    audit.record(f"kill_switch.{key}", entity_type="kill_switch", entity_id=key,
                 payload={"enabled": bool(enabled), "reason": reason},
                 actor_kind=audit.ACTOR_USER, actor_id=actor)


def global_stop_engaged():
    return _switch(GLOBAL_STOP_KEY)


def set_global_stop(enabled, reason=None, actor=None):
    from . import rbac

    rbac.require("provider.kill_switch")
    _set_switch(GLOBAL_STOP_KEY, enabled, reason, actor)


def send_stop_engaged():
    return _switch(SEND_STOP_KEY) or _switch(GLOBAL_STOP_KEY)


def set_send_stop(enabled, reason=None, actor=None):
    from . import rbac

    rbac.require("provider.kill_switch")
    _set_switch(SEND_STOP_KEY, enabled, reason, actor)


# --------------------------------------------------------------------------
# connection + quota state
# --------------------------------------------------------------------------

CONNECTED = "CONNECTED"
NOT_CONNECTED = "NOT_CONNECTED"
QUOTA_LIMITED = "QUOTA_LIMITED"
POLICY_REVIEW_DUE = "POLICY_REVIEW_DUE"

# YouTube Data API quota units per operation, used for pre-flight estimation.
QUOTA_UNIT_COST = {
    ("youtube", "search"): 100,
    ("youtube", "channels.list"): 1,
    ("youtube", "playlists.list"): 1,
    ("youtube", "playlistItems.list"): 1,
    ("youtube", "videos.list"): 1,
}
DEFAULT_QUOTA_LIMIT = {"youtube": 10000}


def connection_state(provider_id, tenant_id=None):
    """The state the UI shows. Never optimistic: missing credentials read as
    NOT CONNECTED even when the policy would otherwise allow the call."""
    from . import config, rbac

    tenant_id = tenant_id or rbac.DEFAULT_TENANT_ID
    policy = get(provider_id)
    row = db.query_one(
        "SELECT * FROM provider_connection WHERE provider = ? AND tenant_id = ?",
        (provider_id, tenant_id),
    )
    creds = config.credentials_present(provider_id)
    required = config.PROVIDER_CREDENTIAL_ENV.get(provider_id)

    if policy.killSwitchEnabled or global_stop_engaged():
        display = DISABLED
    elif policy.status in (BLOCKED, DISABLED):
        display = policy.status
    elif policy.status == MANUAL_ONLY:
        display = MANUAL_ONLY
    elif policy.status == APPROVAL_REQUIRED:
        display = APPROVAL_REQUIRED
    elif required is None:
        display = NOT_CONNECTED
    elif required == [] :
        display = CONNECTED
    elif creds:
        display = CONNECTED
    else:
        display = NOT_CONNECTED

    quota_used = row["quota_used"] if row else 0
    quota_limit = (row["quota_limit"] if row and row["quota_limit"] else
                   DEFAULT_QUOTA_LIMIT.get(provider_id))
    if display == CONNECTED and quota_limit and quota_used >= quota_limit:
        display = QUOTA_LIMITED

    return {
        "provider": provider_id,
        "display_name": policy.displayName or provider_id,
        "policy_status": policy.status,
        "state": display,
        "credentials_present": creds,
        "missing_credentials": config.missing_credentials(provider_id) if required else [],
        "capabilities": policy.capabilities,
        "quota_used": quota_used,
        "quota_limit": quota_limit,
        "quota_pct": round(quota_used / quota_limit * 100) if quota_limit else None,
        "last_success_at": row["last_success_at"] if row else None,
        "last_error": row["last_error"] if row else None,
        "last_error_at": row["last_error_at"] if row else None,
        "kill_switch": policy.killSwitchEnabled,
        "review_due": review_due(provider_id),
        "last_verified_at": policy.lastVerifiedAt,
        "next_review_at": policy.nextReviewAt,
        "retention_days": policy.retentionDays,
        "may_send_to_llm": policy.maySendDataToLanguageModel,
        "may_embed": policy.mayCreateEmbeddings,
        "may_resolve_contacts": policy.mayResolveContactsFromProviderData,
        "attribution_required": policy.attributionRequired,
        "human_action_required": policy.humanActionRequired,
        "notes": policy.notes,
        "policy_source": policy.policySourceName,
    }


def all_connection_states(tenant_id=None):
    seed_policies()
    return [connection_state(p.provider, tenant_id) for p in BASELINE_POLICIES]


def connection_summary(tenant_id=None):
    """State counts across every provider, for a one-line summary in the UI.

    Counts only — never a percentage, and never a green light computed from
    absence of information.
    """
    states = all_connection_states(tenant_id)
    counts = {}
    for state in states:
        counts[state["state"]] = counts.get(state["state"], 0) + 1
    return {
        "total": len(states),
        "connected": counts.get(CONNECTED, 0),
        "by_state": counts,
        "attention": [s for s in states if s["last_error"] or s["kill_switch"]],
    }


def _ensure_connection_row(provider_id, tenant_id):
    row = db.query_one(
        "SELECT * FROM provider_connection WHERE provider = ? AND tenant_id = ?",
        (provider_id, tenant_id),
    )
    if row is None:
        db.insert("provider_connection", {
            "provider": provider_id,
            "tenant_id": tenant_id,
            "status": NOT_CONNECTED,
            "quota_used": 0,
            "quota_limit": DEFAULT_QUOTA_LIMIT.get(provider_id),
            "quota_window_started_at": clock.now_iso(),
            "updated_at": clock.now_iso(),
        })
        row = db.query_one(
            "SELECT * FROM provider_connection WHERE provider = ? AND tenant_id = ?",
            (provider_id, tenant_id),
        )
    return row


def estimate_quota_cost(provider_id, operation):
    return QUOTA_UNIT_COST.get((provider_id, operation), 1)


def check_quota(provider_id, operation, tenant_id=None):
    """Pre-flight: raise before spending units we do not have."""
    from . import rbac

    tenant_id = tenant_id or rbac.DEFAULT_TENANT_ID
    row = _ensure_connection_row(provider_id, tenant_id)
    limit = row["quota_limit"]
    if not limit:
        return {"warn": False, "used": row["quota_used"], "limit": None}
    cost = estimate_quota_cost(provider_id, operation)
    if row["quota_used"] + cost > limit:
        raise QuotaExceeded(
            f"{provider_id} daily quota exhausted ({row['quota_used']}/{limit} units)"
        )
    pct = (row["quota_used"] + cost) / limit
    return {"warn": pct >= 0.8, "used": row["quota_used"], "limit": limit, "pct": pct}


def record_request(provider_id, operation, outcome, http_status=None,
                   cost_units=None, campaign_id=None, tenant_id=None, error=None):
    from . import rbac

    tenant_id = tenant_id or rbac.DEFAULT_TENANT_ID
    cost_units = cost_units if cost_units is not None else estimate_quota_cost(provider_id, operation)
    now = clock.now_iso()
    _ensure_connection_row(provider_id, tenant_id)
    db.insert("provider_request_log", {
        "id": db.new_id("preq"),
        "tenant_id": tenant_id,
        "provider": provider_id,
        "operation": operation,
        "cost_units": cost_units,
        "http_status": http_status,
        "outcome": outcome,
        "campaign_id": campaign_id,
        "created_at": now,
    })
    if outcome == "OK":
        db.execute(
            "UPDATE provider_connection SET quota_used = quota_used + ?, last_success_at = ?, "
            "status = ?, updated_at = ? WHERE provider = ? AND tenant_id = ?",
            (cost_units, now, CONNECTED, now, provider_id, tenant_id),
        )
    elif outcome == "FIXTURE":
        # A fixture run is logged, but it is not an error and must never render
        # as one — the run is labelled FIXTURE everywhere instead.
        db.execute(
            "UPDATE provider_connection SET updated_at = ? WHERE provider = ? AND tenant_id = ?",
            (now, provider_id, tenant_id),
        )
    else:
        db.execute(
            "UPDATE provider_connection SET quota_used = quota_used + ?, last_error = ?, "
            "last_error_at = ?, updated_at = ? WHERE provider = ? AND tenant_id = ?",
            (cost_units, error or outcome, now, now, provider_id, tenant_id),
        )


def reset_quota(provider_id, tenant_id=None):
    from . import rbac

    tenant_id = tenant_id or rbac.DEFAULT_TENANT_ID
    _ensure_connection_row(provider_id, tenant_id)
    db.execute(
        "UPDATE provider_connection SET quota_used = 0, quota_window_started_at = ?, "
        "updated_at = ? WHERE provider = ? AND tenant_id = ?",
        (clock.now_iso(), clock.now_iso(), provider_id, tenant_id),
    )
