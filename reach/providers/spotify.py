"""Spotify adapter — narrowly permitted functions only.

What this adapter is for: user-authorized metadata, links and identifiers, a
campaign destination, an editorial-pitch human action, and placement links where
permitted.

What it is explicitly not: a contact-harvesting engine, a crawl target, or a
source of text for a language model. Those restrictions are enforced by
:mod:`reach.firewall` and by :func:`reach.contacts.classify`, not by convention.
Every record this adapter returns is tagged ``restricted=True`` and
``provider="spotify"`` so downstream code cannot lose track of where it came
from.
"""

import json
import threading
from urllib.parse import quote_plus, urlencode

from .. import config, netguard, policy
from ..errors import ProviderNotConnected
from . import base

PROVIDER = "spotify"
API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"

_token_cache = {}
_token_lock = threading.Lock()

DEVELOPMENT_MODE = "DEVELOPMENT_MODE"
EXTENDED_QUOTA = "EXTENDED_QUOTA"


def connected():
    return config.credentials_present(PROVIDER)


def status():
    state = base.state(PROVIDER)
    state["quota_mode"] = quota_mode()
    return state


def quota_mode():
    """Displayed verbatim on Provider Health. Defaults to the honest answer."""
    declared = (config.env("REACH_SPOTIFY_QUOTA_MODE") or DEVELOPMENT_MODE).upper()
    return EXTENDED_QUOTA if declared == EXTENDED_QUOTA else DEVELOPMENT_MODE


def reset_token_cache():
    with _token_lock:
        _token_cache.clear()


def _access_token(campaign_id=None):
    with _token_lock:
        cached = _token_cache.get("token")
    if cached:
        return cached

    from .soundcloud import _post  # shared validated POST path

    from ..fetcher import get_transport

    validated = netguard.validate_url(TOKEN_URL)
    body = urlencode({
        "grant_type": "client_credentials",
        "client_id": config.env("REACH_SPOTIFY_CLIENT_ID"),
        "client_secret": config.env("REACH_SPOTIFY_CLIENT_SECRET"),
    }).encode("utf-8")
    status_code, _headers, payload = _post(get_transport(), validated, body)
    if status_code >= 400:
        policy.record_request(PROVIDER, "oauth.token", "ERROR", http_status=status_code,
                              campaign_id=campaign_id)
        raise ProviderNotConnected(f"Spotify token exchange failed (HTTP {status_code})")
    token = json.loads(payload.decode("utf-8", errors="replace")).get("access_token")
    if not token:
        raise ProviderNotConnected("Spotify token exchange returned no access_token")
    with _token_lock:
        _token_cache["token"] = token
    policy.record_request(PROVIDER, "oauth.token", "OK", http_status=status_code,
                          campaign_id=campaign_id)
    return token


def _restricted(payload):
    """Tag every record so its provenance survives every hop."""
    payload = dict(payload)
    payload["provider"] = PROVIDER
    payload["restricted"] = True
    payload["may_send_to_llm"] = False
    payload["may_resolve_contacts"] = False
    payload["attribution"] = "Content from Spotify. Attribution and links required."
    return payload


def find_track(isrc=None, title=None, artist=None, campaign_id=None):
    """Resolve a Spotify track link for a recording REACH already owns."""
    base.guard(PROVIDER, policy.CATALOG_SEARCH)
    token = _access_token(campaign_id)
    if isrc:
        query = f"isrc:{isrc}"
    elif title:
        query = f'track:"{title}"' + (f' artist:"{artist}"' if artist else "")
    else:
        raise ProviderNotConnected("find_track needs an ISRC or a title")

    url = f"{API}/search?q={quote_plus(query)}&type=track&limit=5"
    payload = base.get_json(url, headers={"Authorization": f"Bearer {token}"},
                            provider=PROVIDER, operation="search",
                            campaign_id=campaign_id)
    items = []
    for track in ((payload.get("tracks") or {}).get("items") or []):
        items.append(_restricted({
            "external_id": track.get("id"),
            "uri": track.get("uri"),
            "url": (track.get("external_urls") or {}).get("spotify"),
            "name": track.get("name"),
            "isrc": (track.get("external_ids") or {}).get("isrc"),
            "explicit": track.get("explicit"),
            "duration_ms": track.get("duration_ms"),
        }))
    return base.AdapterResponse(PROVIDER, base.LIVE, items)


def playlist_contains_track(playlist_id, track_id, campaign_id=None):
    """Placement verification for a playlist the user can legitimately read.

    Returns evidence-shaped data, not a boolean claim: the caller records what
    was seen and when, and a negative answer is 'not observed', not 'removed'.
    """
    base.guard(PROVIDER, policy.PLACEMENT_MONITORING)
    token = _access_token(campaign_id)
    found = False
    position = None
    offset = 0
    checked = 0
    while offset < 1000:
        url = (f"{API}/playlists/{quote_plus(playlist_id)}/tracks"
               f"?fields=items(track(id)),total&limit=100&offset={offset}")
        payload = base.get_json(url, headers={"Authorization": f"Bearer {token}"},
                                provider=PROVIDER, operation="playlists.tracks",
                                campaign_id=campaign_id)
        items = payload.get("items") or []
        if not items:
            break
        for index, item in enumerate(items):
            checked += 1
            track = (item or {}).get("track") or {}
            if track.get("id") == track_id:
                found = True
                position = offset + index + 1
                break
        if found or len(items) < 100:
            break
        offset += 100
    return {
        "observed": found,
        "position": position,
        "tracks_checked": checked,
        "playlist_url": f"https://open.spotify.com/playlist/{playlist_id}",
        "provider": PROVIDER,
        "restricted": True,
    }


EDITORIAL_PITCH_FIELDS = [
    "track_name", "release_date", "genres", "moods", "styles", "languages",
    "instruments", "song_description", "culture_or_scene", "promotion_plan",
    "artist_hometown", "is_cover", "recording_location",
]


def editorial_pitch_requirements():
    """Spotify editorial pitching happens inside Spotify for Artists. REACH
    prepares the answers; a human submits them."""
    return {
        "provider": PROVIDER,
        "automation_available": False,
        "reason": "Spotify for Artists editorial pitch has no submission API. "
                  "Automating a login-walled form is a Phase One non-goal.",
        "destination_url": "https://artists.spotify.com",
        "fields": EDITORIAL_PITCH_FIELDS,
        "constraints": [
            "One unreleased song per pitch.",
            "Pitch before the release is delivered — at least 7 days ahead.",
            "The track must not have been previously released.",
        ],
    }
