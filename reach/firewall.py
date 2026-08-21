"""Source data and AI firewall.

Data-use permission is enforced *in code* before a model call is constructed —
never by asking a model to behave. Every field that could reach a model carries
the provider it came from; :func:`guard_language_model` refuses the whole call
if any contributing provider forbids model use.

The Spotify rule is the sharpest case: Spotify content must never enter a
language model, an embedding, or model training, and an address seen only on
Spotify is never an outreach route.
"""

import json
import re
from dataclasses import asdict, dataclass

from . import clock, policy
from .errors import FirewallViolation

FIREWALL_VERSION = "1.0.0"


@dataclass
class SourceUsagePolicy:
    sourceId: str
    sourceType: str
    userOwned: bool
    publiclyAccessible: bool
    professionalPurpose: bool
    mayStoreRaw: bool
    mayStoreExcerpt: bool
    mayCreateEmbedding: bool
    maySendToLLM: bool
    mayUseForTraining: bool
    attributionRequired: bool
    deletionOnDisconnect: bool
    retentionDays: int
    policyReason: str
    verifiedAt: str

    def to_json(self):
        return json.dumps(asdict(self), sort_keys=True)


USER_OWNED_SOURCES = {"reach_catalog", "user_input", "local_audio_analysis"}


def source_usage_policy(source_id, source_type="PROVIDER"):
    """Derive a source's usage policy from its provider policy record."""
    if source_id in USER_OWNED_SOURCES:
        return SourceUsagePolicy(
            sourceId=source_id,
            sourceType=source_type,
            userOwned=True,
            publiclyAccessible=False,
            professionalPurpose=True,
            mayStoreRaw=True,
            mayStoreExcerpt=True,
            mayCreateEmbedding=True,
            maySendToLLM=True,
            mayUseForTraining=False,
            attributionRequired=False,
            deletionOnDisconnect=False,
            retentionDays=None,
            policyReason="First-party data owned by the tenant; model use still requires "
                         "a recorded rights attestation.",
            verifiedAt=clock.now_iso(),
        )

    provider_policy = policy.get(source_id)
    return SourceUsagePolicy(
        sourceId=source_id,
        sourceType=source_type,
        userOwned=False,
        publiclyAccessible=provider_policy.webDiscoveryAllowed,
        professionalPurpose=True,
        mayStoreRaw=provider_policy.mayStoreRawData,
        mayStoreExcerpt=True,
        mayCreateEmbedding=provider_policy.mayCreateEmbeddings,
        maySendToLLM=provider_policy.maySendDataToLanguageModel,
        mayUseForTraining=provider_policy.mayUseForModelTraining,
        attributionRequired=provider_policy.attributionRequired,
        deletionOnDisconnect=provider_policy.disconnectDeletionRequired,
        retentionDays=provider_policy.retentionDays,
        policyReason=provider_policy.policySourceName,
        verifiedAt=provider_policy.lastVerifiedAt,
    )


# --------------------------------------------------------------------------
# secret detection
# --------------------------------------------------------------------------

_SECRET_PATTERNS = [
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE), "bearer-token"),
    (re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|proj)?[-_]?[A-Za-z0-9]{16,}"), "api-key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws-access-key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "github-token"),
    (re.compile(r"\b[Cc]ookie\s*:\s*\S+"), "cookie-header"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private-key"),
    (re.compile(r"\brefresh_token\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{16,}"), "refresh-token"),
    (re.compile(r"\baccess_token\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{16,}"), "access-token"),
]


def find_secrets(text):
    if not text:
        return []
    return sorted({label for pattern, label in _SECRET_PATTERNS if pattern.search(text)})


def assert_no_secrets(text, context="payload"):
    found = find_secrets(text)
    if found:
        raise FirewallViolation(
            f"Refusing to send {context} to a model: contains {', '.join(found)}"
        )


# --------------------------------------------------------------------------
# guards
# --------------------------------------------------------------------------

def _flatten(text_or_parts):
    if text_or_parts is None:
        return ""
    if isinstance(text_or_parts, str):
        return text_or_parts
    return "\n".join(str(part) for part in text_or_parts if part is not None)


def guard_language_model(source_ids, payload=None, purpose="inference"):
    """Raise unless *every* contributing source permits model use.

    ``source_ids`` is the set of providers that contributed any part of the
    payload. One forbidden source poisons the whole call — there is no partial
    send, because a model cannot be trusted to ignore part of its context.
    """
    blocked = []
    for source_id in sorted(set(source_ids or [])):
        usage = source_usage_policy(source_id)
        if not usage.maySendToLLM:
            blocked.append((source_id, usage.policyReason))
    if blocked:
        detail = "; ".join(f"{sid} ({reason})" for sid, reason in blocked)
        raise FirewallViolation(
            f"Language-model {purpose} blocked — these sources forbid model use: {detail}"
        )
    assert_no_secrets(_flatten(payload), "model context")
    return True


def guard_embedding(source_ids):
    blocked = [
        sid for sid in sorted(set(source_ids or []))
        if not source_usage_policy(sid).mayCreateEmbedding
    ]
    if blocked:
        raise FirewallViolation(
            f"Embedding blocked — restricted provider data may not enter a vector index: "
            f"{', '.join(blocked)}"
        )
    return True


def guard_training(source_ids, consent_recorded=False):
    if not consent_recorded:
        raise FirewallViolation(
            "Model training on campaign data requires separate explicit consent"
        )
    blocked = [
        sid for sid in sorted(set(source_ids or []))
        if not source_usage_policy(sid).mayUseForTraining
    ]
    if blocked:
        raise FirewallViolation(f"Training blocked for sources: {', '.join(blocked)}")
    return True


def guard_contact_resolution(source_id):
    """May a contact route be derived from this provider's data at all?"""
    usage_policy = policy.get(source_id)
    if not usage_policy.mayResolveContactsFromProviderData:
        raise FirewallViolation(
            f"{source_id} data may not be used to resolve an outreach contact "
            f"({usage_policy.policySourceName})"
        )
    return True


def may_resolve_contacts(source_id):
    return policy.get(source_id).mayResolveContactsFromProviderData


def may_store_raw(source_id):
    return source_usage_policy(source_id).mayStoreRaw


def retention_days(source_id):
    return source_usage_policy(source_id).retentionDays


def excerpt(text, limit=400):
    """Store a bounded excerpt, never a full copyrighted page."""
    if not text:
        return None
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "…"
