"""SoundCloud adapter — read-oriented discovery only.

Phase One does not perform account actions. The OAuth client-credentials flow is
implemented because discovery needs a token; nothing here writes.
"""

import json
from urllib.parse import quote_plus, urlencode

from .. import config, netguard, policy
from ..errors import ProviderNotConnected
from . import base

PROVIDER = "soundcloud"
API = "https://api.soundcloud.com"
TOKEN_URL = "https://secure.soundcloud.com/oauth/token"

_token_cache = {}


def connected():
    return config.credentials_present(PROVIDER)


def status():
    return base.state(PROVIDER)


def _access_token(campaign_id=None):
    cached = _token_cache.get("token")
    if cached:
        return cached
    from ..fetcher import get_transport

    validated = netguard.validate_url(TOKEN_URL)
    body = urlencode({
        "grant_type": "client_credentials",
        "client_id": config.env("REACH_SOUNDCLOUD_CLIENT_ID"),
        "client_secret": config.env("REACH_SOUNDCLOUD_CLIENT_SECRET"),
    }).encode("utf-8")
    # The shared transport is GET-only; token exchange uses its own POST path.
    status_code, _headers, payload = _post(get_transport(), validated, body)
    if status_code >= 400:
        policy.record_request(PROVIDER, "oauth.token", "ERROR", http_status=status_code,
                              campaign_id=campaign_id)
        raise ProviderNotConnected(f"SoundCloud token exchange failed (HTTP {status_code})")
    token = json.loads(payload.decode("utf-8", errors="replace")).get("access_token")
    if not token:
        raise ProviderNotConnected("SoundCloud token exchange returned no access_token")
    _token_cache["token"] = token
    policy.record_request(PROVIDER, "oauth.token", "OK", http_status=status_code,
                          campaign_id=campaign_id)
    return token


def _post(transport, validated, body):
    import http.client
    import socket
    import ssl

    family, address = validated["addresses"][0]
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(config.FETCH_TIMEOUT_SECONDS)
    try:
        sock.connect((address, validated["port"]))
        if validated["scheme"] == "https":
            sock = ssl.create_default_context().wrap_socket(
                sock, server_hostname=validated["hostname"]
            )
        conn = http.client.HTTPConnection(validated["hostname"], validated["port"],
                                          timeout=config.FETCH_TIMEOUT_SECONDS)
        conn.sock = sock
        conn.request("POST", validated["path"], body=body, headers={
            "Host": validated["hostname"],
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": config.USER_AGENT,
            "Accept": "application/json",
        })
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read(200_000)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def reset_token_cache():
    _token_cache.clear()


def search_playlists(query, limit=10, campaign_id=None):
    base.guard(PROVIDER, policy.PLAYLIST_DISCOVERY)
    token = _access_token(campaign_id)
    url = f"{API}/playlists?q={quote_plus(query)}&limit={min(limit, 50)}&access=playable"
    payload = base.get_json(url, headers={"Authorization": f"OAuth {token}"},
                            provider=PROVIDER, operation="playlists.search",
                            campaign_id=campaign_id)
    items = []
    for item in payload if isinstance(payload, list) else payload.get("collection", []):
        user = item.get("user") or {}
        items.append({
            "external_id": str(item.get("id")),
            "name": item.get("title"),
            "description": item.get("description"),
            "url": item.get("permalink_url"),
            "track_count": item.get("track_count"),
            "genre": item.get("genre"),
            "curator_name": user.get("username"),
            "curator_url": user.get("permalink_url"),
            "last_modified": item.get("last_modified"),
        })
    return base.AdapterResponse(PROVIDER, base.LIVE, items)


def search_users(query, limit=10, campaign_id=None):
    base.guard(PROVIDER, policy.CURATOR_DISCOVERY)
    token = _access_token(campaign_id)
    url = f"{API}/users?q={quote_plus(query)}&limit={min(limit, 50)}"
    payload = base.get_json(url, headers={"Authorization": f"OAuth {token}"},
                            provider=PROVIDER, operation="users.search",
                            campaign_id=campaign_id)
    items = []
    for item in payload if isinstance(payload, list) else payload.get("collection", []):
        items.append({
            "external_id": str(item.get("id")),
            "name": item.get("username"),
            "description": item.get("description"),
            "url": item.get("permalink_url"),
            "followers": item.get("followers_count"),
            "city": item.get("city"),
            "country": item.get("country"),
            "website": item.get("website"),
        })
    return base.AdapterResponse(PROVIDER, base.LIVE, items)
