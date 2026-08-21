"""Mixcloud adapter — show, DJ, channel and tag discovery."""

from urllib.parse import quote_plus

from .. import config, policy
from . import base

PROVIDER = "mixcloud"
API = "https://api.mixcloud.com"


def connected():
    return config.credentials_present(PROVIDER)


def status():
    return base.state(PROVIDER)


def search_users(query, limit=10, campaign_id=None):
    base.guard(PROVIDER, policy.CURATOR_DISCOVERY)
    url = f"{API}/search/?q={quote_plus(query)}&type=user&limit={min(limit, 50)}"
    payload = base.get_json(url, provider=PROVIDER, operation="search.user",
                            campaign_id=campaign_id)
    items = []
    for item in payload.get("data", []):
        items.append({
            "external_id": item.get("key"),
            "name": item.get("name"),
            "url": item.get("url"),
            "city": item.get("city"),
            "country": item.get("country"),
            "followers": item.get("follower_count"),
            "biog": item.get("biog"),
        })
    return base.AdapterResponse(PROVIDER, base.LIVE, items)


def search_shows(query, limit=10, campaign_id=None):
    base.guard(PROVIDER, policy.CATALOG_SEARCH)
    url = f"{API}/search/?q={quote_plus(query)}&type=cloudcast&limit={min(limit, 50)}"
    payload = base.get_json(url, provider=PROVIDER, operation="search.cloudcast",
                            campaign_id=campaign_id)
    items = []
    for item in payload.get("data", []):
        user = item.get("user") or {}
        items.append({
            "external_id": item.get("key"),
            "name": item.get("name"),
            "url": item.get("url"),
            "created_time": item.get("created_time"),
            "tags": [tag.get("name") for tag in item.get("tags", []) if tag.get("name")],
            "curator_name": user.get("name"),
            "curator_url": user.get("url"),
        })
    return base.AdapterResponse(PROVIDER, base.LIVE, items)
