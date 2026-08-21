"""Runtime configuration for REACH.

Every value is read from the process environment. Nothing here is ever sent to
the browser: templates receive derived booleans ("credentials present") and
never the credential itself.
"""

import os

# --- store -----------------------------------------------------------------

DB_PATH_ENV = "REACH_DB_PATH"
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reach.db")

# --- encryption ------------------------------------------------------------

ENCRYPTION_KEY_ENV = "REACH_ENCRYPTION_KEY"

# --- sender identity -------------------------------------------------------

SENDER_DOMAIN_ENV = "REACH_SENDER_DOMAIN"
SENDER_FROM_ENV = "REACH_SENDER_FROM"
SENDER_NAME_ENV = "REACH_SENDER_NAME"
SENDER_POSTAL_ADDRESS_ENV = "REACH_SENDER_POSTAL_ADDRESS"
SENDER_COUNTRY_ENV = "REACH_SENDER_COUNTRY"

# --- provider credentials --------------------------------------------------
# Each entry maps a provider id to the env vars that must ALL be present for
# the adapter to consider itself credentialed. Missing credentials produce a
# NOT_CONNECTED state; they never produce fabricated results.

PROVIDER_CREDENTIAL_ENV = {
    # An empty list means "works with no credential" — reported as CONNECTED.
    # A provider absent from this map has no adapter and reads NOT_CONNECTED.
    "reach_catalog": [],         # REACH's own catalog, always available
    "open_web_fetch": [],        # the fetcher itself needs no credential
    "open_web_search": ["REACH_SEARCH_API_KEY"],
    "youtube": ["REACH_YOUTUBE_API_KEY"],
    "soundcloud": ["REACH_SOUNDCLOUD_CLIENT_ID", "REACH_SOUNDCLOUD_CLIENT_SECRET"],
    "mixcloud": ["REACH_MIXCLOUD_CLIENT_ID", "REACH_MIXCLOUD_CLIENT_SECRET"],
    "musicbrainz": [],          # public API, no credential, requires User-Agent
    "listenbrainz": [],         # public endpoints only in Phase One
    "spotify": ["REACH_SPOTIFY_CLIENT_ID", "REACH_SPOTIFY_CLIENT_SECRET"],
    "email": ["REACH_EMAIL_API_KEY"],
    "language_model": ["REACH_LLM_API_KEY"],
}

# Domains that are platforms, marketplaces, encyclopedias, code hosts,
# stock/AI-music services or listicle directories. Pages here rank for "submit
# music" constantly, but the domain's operator is not an outlet a campaign can
# pitch — when there is an outlet, it is whoever runs a profile ON the platform,
# which Phase One does not model. Deliberately absent: bandcamp.com, where the
# page owner (a label or artist) genuinely is the outlet.
PLATFORM_DOMAINS = {
    # DSPs and big platforms
    "spotify.com", "apple.com", "youtube.com", "youtu.be", "soundcloud.com",
    "deezer.com", "tidal.com", "amazon.com", "pandora.com", "last.fm",
    # social
    "facebook.com", "instagram.com", "tiktok.com", "twitter.com", "x.com",
    "reddit.com", "linkedin.com", "pinterest.com", "discord.com",
    # encyclopedias, code hosts, marketplaces
    "wikipedia.org", "fandom.com", "github.com", "gitlab.com",
    "fiverr.com", "upwork.com", "etsy.com",
    # stock / AI music and creator tools — they sell music, they do not take it
    "epidemicsound.com", "artlist.io", "audiojungle.net", "envato.com",
    "pixabay.com", "premiumbeat.com", "pond5.com", "mubert.com",
    "elevenlabs.io", "soundstripe.com",
    # listicle and directory aggregators
    "feedspot.com", "toolify.ai", "player.fm",
}

# Search backend selection: "brave" | "google_cse". Only consulted when the
# search credential is present.
SEARCH_BACKEND_ENV = "REACH_SEARCH_BACKEND"
SEARCH_CSE_ID_ENV = "REACH_SEARCH_CSE_ID"

# --- crawl budgets ---------------------------------------------------------

USER_AGENT = (
    "ReachBot/1.0 (+https://streetbanker.example/reach-bot; music opportunity discovery)"
)
FETCH_TIMEOUT_SECONDS = 10
FETCH_MAX_BYTES = 2_000_000
FETCH_MAX_REDIRECTS = 3
DEFAULT_DOMAIN_BUDGET = 8
DEFAULT_SEARCH_BUDGET = 24
DEFAULT_DAILY_SEND_LIMIT = 50

# --- feature flags ---------------------------------------------------------
# Phase One non-goals stay off. They exist so the surrounding code has a real
# switch to test against, not so they can be turned on casually.

FEATURE_FLAGS = {
    "reach.autopilot": False,
    "reach.automated_form_submission": False,
    "reach.captcha_solving": False,
    "reach.authenticated_browser_automation": False,
    "reach.social_dm_automation": False,
    "reach.automated_reply_sending": False,
    "reach.autonomous_spend": False,
    "reach.contact_graph_export": False,
    "reach.model_training_on_campaign_data": False,
    "reach.copilot_email": True,
    "reach.scout": True,
}


def env(name, default=None):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def credentials_present(provider_id):
    """True only when every env var the adapter needs is actually set."""
    required = PROVIDER_CREDENTIAL_ENV.get(provider_id)
    if required is None:
        return False
    return all(env(name) for name in required)


def missing_credentials(provider_id):
    return [name for name in PROVIDER_CREDENTIAL_ENV.get(provider_id, []) if not env(name)]


def feature_enabled(flag):
    override = env("REACH_FLAG_" + flag.replace(".", "_").upper())
    if override is not None:
        return override.lower() in ("1", "true", "yes", "on")
    return FEATURE_FLAGS.get(flag, False)


def db_path():
    return env(DB_PATH_ENV, DEFAULT_DB_PATH)


def sender_country():
    return env(SENDER_COUNTRY_ENV, "US")
