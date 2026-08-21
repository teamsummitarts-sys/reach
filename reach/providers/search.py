"""Open-web search adapter.

Supports two credentialed backends (Brave Search API, Google Programmable
Search) and a fixture backend used when no credential is configured. The fixture
backend is always reported as ``mode=FIXTURE`` so a Scout run can say plainly
that it searched a fixture corpus rather than the live web.

Claude's own web-search capability is deliberately not used as the production
search backend — the campaign needs a provider with a contract, a quota and an
auditable request log.
"""

from urllib.parse import quote_plus

from .. import config, policy
from ..fixtures import search as search_fixtures
from . import base

PROVIDER = "open_web_search"

BRAVE = "brave"
GOOGLE_CSE = "google_cse"


def backend():
    configured = (config.env(config.SEARCH_BACKEND_ENV) or BRAVE).lower()
    return configured if configured in (BRAVE, GOOGLE_CSE) else BRAVE


def connected():
    return config.credentials_present(PROVIDER)


def status():
    return base.state(PROVIDER)


def search(query, limit=10, campaign_id=None):
    """Run one query. Falls back to the fixture corpus, clearly labelled."""
    policy.require_capability(PROVIDER, policy.CURATOR_DISCOVERY)

    if not connected():
        results = search_fixtures.search(query, limit=limit)
        policy.record_request(PROVIDER, "search", "FIXTURE", campaign_id=campaign_id,
                              cost_units=0)
        return base.AdapterResponse(
            PROVIDER, base.FIXTURE, results,
            note="No search credential configured (REACH_SEARCH_API_KEY). "
                 "Results come from the built-in fixture corpus.",
        )

    policy.check_quota(PROVIDER, "search")
    if backend() == GOOGLE_CSE:
        return _google(query, limit, campaign_id)
    return _brave(query, limit, campaign_id)


def _brave(query, limit, campaign_id):
    url = (
        "https://api.search.brave.com/res/v1/web/search"
        f"?q={quote_plus(query)}&count={min(limit, 20)}"
    )
    payload = base.get_json(
        url,
        headers={
            "X-Subscription-Token": config.env("REACH_SEARCH_API_KEY"),
            "Accept": "application/json",
        },
        provider=PROVIDER,
        operation="search",
        campaign_id=campaign_id,
    )
    items = []
    for rank, item in enumerate((payload.get("web") or {}).get("results") or [], start=1):
        items.append({
            "rank": rank,
            "url": item.get("url"),
            "title": item.get("title"),
            "snippet": item.get("description"),
        })
    return base.AdapterResponse(PROVIDER, base.LIVE, items[:limit], raw=None)


def _google(query, limit, campaign_id):
    cse_id = config.env(config.SEARCH_CSE_ID_ENV)
    if not cse_id:
        raise base.ProviderNotConnected(
            "Google Programmable Search needs REACH_SEARCH_CSE_ID"
        )
    url = (
        "https://www.googleapis.com/customsearch/v1"
        f"?key={quote_plus(config.env('REACH_SEARCH_API_KEY'))}"
        f"&cx={quote_plus(cse_id)}&q={quote_plus(query)}&num={min(limit, 10)}"
    )
    payload = base.get_json(url, provider=PROVIDER, operation="search",
                            campaign_id=campaign_id)
    items = []
    for rank, item in enumerate(payload.get("items") or [], start=1):
        items.append({
            "rank": rank,
            "url": item.get("link"),
            "title": item.get("title"),
            "snippet": item.get("snippet"),
        })
    return base.AdapterResponse(PROVIDER, base.LIVE, items[:limit])
