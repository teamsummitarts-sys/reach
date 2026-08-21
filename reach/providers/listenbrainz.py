"""ListenBrainz adapter — public metadata and popularity endpoints only.

Listener data is personal data. Phase One reads only public or explicitly
user-authorized endpoints, and stores aggregate popularity rather than
individual listening records.
"""

from urllib.parse import quote_plus

from .. import policy
from . import base

PROVIDER = "listenbrainz"
API = "https://api.listenbrainz.org/1"


def connected():
    return True  # public endpoints; a token is only needed for user data


def status():
    return base.state(PROVIDER)


def popularity(musicbrainz_recording_id, campaign_id=None):
    """Aggregate listen/listener counts for a recording, if published."""
    policy.require_capability(PROVIDER, policy.CATALOG_SEARCH)
    url = (
        f"{API}/popularity/recording?recording_mbids="
        f"{quote_plus(musicbrainz_recording_id)}"
    )
    payload = base.get_json(url, provider=PROVIDER, operation="popularity.recording",
                            campaign_id=campaign_id)
    items = []
    rows = payload if isinstance(payload, list) else payload.get("payload", [])
    for row in rows:
        items.append({
            "recording_mbid": row.get("recording_mbid"),
            "total_listen_count": row.get("total_listen_count"),
            "total_user_count": row.get("total_user_count"),
        })
    return base.AdapterResponse(PROVIDER, base.LIVE, items)
