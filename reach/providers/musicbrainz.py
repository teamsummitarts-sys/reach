"""MusicBrainz adapter — canonical recording identity.

MusicBrainz needs no credential but does require an identifying User-Agent and
a one-request-per-second ceiling, both of which are enforced here.

A MusicBrainz id strengthens identity. It is not evidence of rights ownership,
and REACH never treats it as such.
"""

import threading
import time
from urllib.parse import quote_plus

from .. import policy
from . import base

PROVIDER = "musicbrainz"
API = "https://musicbrainz.org/ws/2"

_rate_lock = threading.Lock()
_last_request = [0.0]
MIN_INTERVAL_SECONDS = 1.0


def connected():
    return True  # public API, no credential required


def status():
    return base.state(PROVIDER)


def _throttle():
    with _rate_lock:
        elapsed = time.monotonic() - _last_request[0]
        if elapsed < MIN_INTERVAL_SECONDS:
            wait = MIN_INTERVAL_SECONDS - elapsed
            from ..fetcher import using_fixtures
            if not using_fixtures():  # pragma: no cover - timing path
                time.sleep(wait)
        _last_request[0] = time.monotonic()


def lookup_by_isrc(isrc, campaign_id=None):
    policy.require_capability(PROVIDER, policy.CATALOG_SEARCH)
    _throttle()
    url = f"{API}/isrc/{quote_plus(isrc)}?fmt=json&inc=artist-credits+releases"
    payload = base.get_json(url, provider=PROVIDER, operation="isrc.lookup",
                            campaign_id=campaign_id)
    items = []
    for recording in payload.get("recordings", []):
        items.append({
            "musicbrainz_recording_id": recording.get("id"),
            "title": recording.get("title"),
            "length_ms": recording.get("length"),
            "artist_credit": [
                credit.get("name") for credit in recording.get("artist-credit", [])
                if isinstance(credit, dict) and credit.get("name")
            ],
            "releases": [
                {"id": release.get("id"), "title": release.get("title"),
                 "date": release.get("date")}
                for release in recording.get("releases", [])
            ],
        })
    return base.AdapterResponse(PROVIDER, base.LIVE, items)


def search_recording(title, artist=None, campaign_id=None):
    policy.require_capability(PROVIDER, policy.CATALOG_SEARCH)
    _throttle()
    query = f'recording:"{title}"'
    if artist:
        query += f' AND artist:"{artist}"'
    url = f"{API}/recording?query={quote_plus(query)}&fmt=json&limit=5"
    payload = base.get_json(url, provider=PROVIDER, operation="recording.search",
                            campaign_id=campaign_id)
    items = []
    for recording in payload.get("recordings", []):
        items.append({
            "musicbrainz_recording_id": recording.get("id"),
            "title": recording.get("title"),
            "score": recording.get("score"),
            "length_ms": recording.get("length"),
            "artist_credit": [
                credit.get("name") for credit in recording.get("artist-credit", [])
                if isinstance(credit, dict) and credit.get("name")
            ],
        })
    return base.AdapterResponse(PROVIDER, base.LIVE, items)
