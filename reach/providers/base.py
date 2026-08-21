"""Shared adapter plumbing: policy gate, JSON transport, quota accounting."""

import json

from .. import config, netguard, policy
from ..errors import ProviderNotConnected, ProviderRateLimited

FIXTURE = "FIXTURE"
LIVE = "LIVE"


class AdapterResponse:
    """Carries the provenance mode so nothing downstream can mistake a fixture
    result for a live one."""

    __slots__ = ("provider", "mode", "items", "raw", "note")

    def __init__(self, provider, mode, items, raw=None, note=None):
        self.provider = provider
        self.mode = mode
        self.items = items
        self.raw = raw
        self.note = note

    @property
    def is_live(self):
        return self.mode == LIVE

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)


def guard(provider_id, capability):
    """Policy first, credentials second. Both must pass before any request."""
    policy.require_capability(provider_id, capability)
    if not config.credentials_present(provider_id):
        missing = config.missing_credentials(provider_id)
        raise ProviderNotConnected(
            f"{provider_id} is NOT CONNECTED — missing {', '.join(missing) or 'credentials'}"
        )


def get_json(url, headers=None, provider=None, operation="request", campaign_id=None):
    """A validated GET that expects JSON. Used for provider APIs, not crawling.

    Robots does not apply to a documented API endpoint, but every other egress
    control does: scheme, port, DNS validation, size cap and MIME check.
    """
    from ..fetcher import get_transport, using_fixtures

    validated = netguard.validate_url(url, resolve=not using_fixtures())
    request_headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    request_headers.update(headers or {})

    status, response_headers, body = get_transport().request(validated, request_headers)

    if status == 429:
        retry_after = response_headers.get("Retry-After") or response_headers.get("retry-after")
        if provider:
            policy.record_request(provider, operation, "RATE_LIMITED", http_status=429,
                                  campaign_id=campaign_id)
        raise ProviderRateLimited(
            f"{provider or url} returned 429", retry_after=_parse_retry_after(retry_after)
        )

    netguard.check_size(len(body))
    if status >= 400:
        if provider:
            policy.record_request(provider, operation, "ERROR", http_status=status,
                                  campaign_id=campaign_id, error=f"HTTP {status}")
        raise ProviderNotConnected(f"{provider or url} returned HTTP {status}")

    if provider:
        policy.record_request(provider, operation, "OK", http_status=status,
                              campaign_id=campaign_id)
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise ProviderNotConnected(f"{provider or url} returned non-JSON: {exc}")


def _parse_retry_after(value):
    if not value:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def state(provider_id):
    return policy.connection_state(provider_id)
