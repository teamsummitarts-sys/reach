"""Provider adapters.

Every adapter follows the same contract:

* it asks :mod:`reach.policy` whether the capability is permitted at all;
* it refuses with :class:`ProviderNotConnected` when its credentials are absent
  — it never fabricates a response;
* it records quota and request outcomes so Provider Health reflects reality.
"""

from . import (  # noqa: F401
    base, email, listenbrainz, mixcloud, musicbrainz, search, soundcloud, spotify, youtube,
)

ADAPTERS = {
    "open_web_search": search,
    "youtube": youtube,
    "soundcloud": soundcloud,
    "mixcloud": mixcloud,
    "musicbrainz": musicbrainz,
    "listenbrainz": listenbrainz,
    "spotify": spotify,
    "email": email,
}
