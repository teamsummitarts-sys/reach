"""YouTube Data API adapter.

Quota is the constraint that matters: a single ``search.list`` costs 100 units
of a 10,000-unit daily default, while ``channels.list`` costs 1. Every call is
priced before it is made, and the connection row carries the running total so
the UI can warn before exhaustion rather than after.

The API does not expose private creator contact information. This adapter never
claims otherwise: business routes come from independently published pages, or
the opportunity becomes a human task.
"""

from urllib.parse import quote_plus

from .. import config, policy
from . import base

PROVIDER = "youtube"
API = "https://www.googleapis.com/youtube/v3"


def connected():
    return config.credentials_present(PROVIDER)


def status():
    return base.state(PROVIDER)


def _key():
    return config.env("REACH_YOUTUBE_API_KEY")


def search_channels(query, limit=10, campaign_id=None):
    base.guard(PROVIDER, policy.CURATOR_DISCOVERY)
    quota = policy.check_quota(PROVIDER, "search")
    url = (
        f"{API}/search?part=snippet&type=channel&maxResults={min(limit, 25)}"
        f"&q={quote_plus(query)}&key={quote_plus(_key())}"
    )
    payload = base.get_json(url, provider=PROVIDER, operation="search",
                            campaign_id=campaign_id)
    items = []
    for item in payload.get("items", []):
        snippet = item.get("snippet") or {}
        channel_id = (item.get("id") or {}).get("channelId")
        items.append({
            "external_id": channel_id,
            "name": snippet.get("channelTitle") or snippet.get("title"),
            "description": snippet.get("description"),
            "url": f"https://www.youtube.com/channel/{channel_id}" if channel_id else None,
            "published_at": snippet.get("publishedAt"),
        })
    response = base.AdapterResponse(PROVIDER, base.LIVE, items)
    response.note = "quota warning" if quota.get("warn") else None
    return response


def search_playlists(query, limit=10, campaign_id=None):
    base.guard(PROVIDER, policy.PLAYLIST_DISCOVERY)
    policy.check_quota(PROVIDER, "search")
    url = (
        f"{API}/search?part=snippet&type=playlist&maxResults={min(limit, 25)}"
        f"&q={quote_plus(query)}&key={quote_plus(_key())}"
    )
    payload = base.get_json(url, provider=PROVIDER, operation="search",
                            campaign_id=campaign_id)
    items = []
    for item in payload.get("items", []):
        snippet = item.get("snippet") or {}
        playlist_id = (item.get("id") or {}).get("playlistId")
        items.append({
            "external_id": playlist_id,
            "name": snippet.get("title"),
            "description": snippet.get("description"),
            "url": f"https://www.youtube.com/playlist?list={playlist_id}" if playlist_id else None,
            "channel_title": snippet.get("channelTitle"),
            "published_at": snippet.get("publishedAt"),
        })
    return base.AdapterResponse(PROVIDER, base.LIVE, items)


def get_channel(channel_id, campaign_id=None):
    base.guard(PROVIDER, policy.PUBLIC_PROFILE_DISCOVERY)
    policy.check_quota(PROVIDER, "channels.list")
    url = (
        f"{API}/channels?part=snippet,statistics&id={quote_plus(channel_id)}"
        f"&key={quote_plus(_key())}"
    )
    payload = base.get_json(url, provider=PROVIDER, operation="channels.list",
                            campaign_id=campaign_id)
    items = []
    for item in payload.get("items", []):
        snippet = item.get("snippet") or {}
        stats = item.get("statistics") or {}
        items.append({
            "external_id": item.get("id"),
            "name": snippet.get("title"),
            "description": snippet.get("description"),
            "country": snippet.get("country"),
            "published_at": snippet.get("publishedAt"),
            "subscriber_count": _int(stats.get("subscriberCount")),
            "video_count": _int(stats.get("videoCount")),
            "url": f"https://www.youtube.com/channel/{item.get('id')}",
            # Deliberately absent: any contact field. The API does not provide one.
            "contact_email": None,
        })
    return base.AdapterResponse(PROVIDER, base.LIVE, items)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def quota_report(tenant_id=None):
    state = policy.connection_state(PROVIDER, tenant_id)
    return {
        "used": state["quota_used"],
        "limit": state["quota_limit"],
        "pct": state["quota_pct"],
        "warn": bool(state["quota_pct"] is not None and state["quota_pct"] >= 80),
        "search_cost": policy.estimate_quota_cost(PROVIDER, "search"),
        "list_cost": policy.estimate_quota_cost(PROVIDER, "channels.list"),
    }
