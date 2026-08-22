"""Paid promotion: research, screening and spend planning.

REACH researches services an artist can legitimately pay for — submission
access, curator consideration, professional review, PR, advertising, campaign
administration — screens them deterministically against public evidence, ranks
their fit for a campaign *separately* from their safety, and hands the actual
submission or purchase to a person.

The category line this module exists to hold: paying for access, consideration,
advertising or professional promotion is a real part of music promotion; paying
for guaranteed streams, guaranteed placement, saves, followers, algorithmic
triggering or bot engagement is not. The first is screened. The second is
BLOCKED, with the page's own words as the receipt.

Everything here is deterministic: regex rule tables with negation and
offer-voice guards, threshold logic, and the existing crawler stack. No model is
involved, so page text can never act as an instruction. Where evidence does not
establish a fact, the fact stays UNKNOWN — which never means safe.

Three readings of the honesty rules are worth stating because they shaped the
code:

* **Supersession.** Facts are recomputed from the *current* packets — the most
  recent read of each URL. A page that drops a claim drops the fact it backed;
  a page that never mentioned the fact erases nothing another page established.
* **Blocking is first-party only.** Phase One blocks a service on evidence from
  the service's own fetched pages, in its own offer voice. A warning article
  about a service is not evidence against it, and a search snippet never is.
* **The ladder starts at UNKNOWN.** SCREENED and CAUTION are reachable only when
  their full conditions hold; there is no fall-through to SCREENED.
"""

import json
import re
import time
from urllib.parse import urlsplit

from . import (audit, campaigns, clock, config, crypto, db, entities, evidence,
               extractor, fetcher, jobs, netguard, profile, rbac, sanitizer)
from .errors import FetchBlocked, ValidationError
from .providers import search as search_provider

PROMO_VERSION = "promo-screen/1.0.0"
FIT_VERSION = "service-fit/1.0.0"

# --- budgets and cadence ----------------------------------------------------
# Caps are stop rules, not failures: a spent cap records a skip receipt and the
# page states the spend against the cap.

SWEEP_SEARCH_CAP = 40
RESULTS_PER_SEARCH = 8
PROMO_SWEEP_STALE_DAYS = 14


def _env_days(name, default):
    raw = config.env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


RESCREEN_DAYS_SCREENED = _env_days("REACH_PROMO_RESCREEN_SCREENED", 30)
RESCREEN_DAYS_CAUTION = _env_days("REACH_PROMO_RESCREEN_CAUTION", 14)
RESCREEN_DAYS_BLOCKED = _env_days("REACH_PROMO_RESCREEN_BLOCKED", 7)
RESCREEN_DAYS_UNKNOWN = _env_days("REACH_PROMO_RESCREEN_UNKNOWN", 14)

# Seed *queries* only. Naming a service here never pre-creates it, never
# pre-trusts it and never whitelists it: a seeded name goes through exactly the
# same live evidence process as anything discovered. These platforms also hold
# APPROVAL_REQUIRED provider policies (policy.FUTURE_PROVIDERS) — their
# proprietary databases stay uncrawled; only their public marketing, pricing and
# terms pages are read here, through the open-web search and fetch providers.
SEED_SERVICES = ["SubmitHub", "Playlist Push", "Groover", "SoundCampaign", "Musosoup"]

# DSP policy pages, fetched once per sweep and refetched when their evidence
# goes stale, so "incompatible with platform policy" is backed by the platform's
# own current words rather than by text hard-coded here.
PLATFORM_POLICY_URLS = {
    "spotify-artificial-streaming":
        "https://artists.spotify.com/help/article/what-is-artificial-streaming",
    "spotify-third-party-promotion":
        "https://artists.spotify.com/help/article/third-party-promotion-services",
}

# --- vocabularies -----------------------------------------------------------

SCREENED = "SCREENED"
CAUTION = "CAUTION"
UNKNOWN = "UNKNOWN"
BLOCKED = "BLOCKED"
SCREENING_STATUSES = [SCREENED, CAUTION, UNKNOWN, BLOCKED]

# Worse wins, exactly like compliance._SEVERITY: an automated pass may tighten
# past a human override, never loosen one.
_SEVERITY = {BLOCKED: 3, CAUTION: 2, UNKNOWN: 1, SCREENED: 0}

TRUE = "TRUE"
FALSE = "FALSE"
TRISTATE_UNKNOWN = "UNKNOWN"

PLATFORM = "PLATFORM"
PROMOTION_COMPANY = "PROMOTION_COMPANY"
INDIVIDUAL_CURATOR = "INDIVIDUAL_CURATOR"
PAID_MEDIA_OUTLET = "PAID_MEDIA_OUTLET"
AD_PLATFORM = "AD_PLATFORM"
RADIO_PROMOTION = "RADIO_PROMOTION"
CATEGORY_UNKNOWN = "UNKNOWN"
CATEGORIES = [PLATFORM, PROMOTION_COMPANY, INDIVIDUAL_CURATOR, PAID_MEDIA_OUTLET,
              AD_PLATFORM, RADIO_PROMOTION, CATEGORY_UNKNOWN]

# Where playlist/editorial placement is the thing being bought, discretion over
# it is load-bearing. An unclassified service is treated as placement-relevant —
# the conservative reading.
PLACEMENT_RELEVANT = {PLATFORM, PROMOTION_COMPANY, INDIVIDUAL_CURATOR, CATEGORY_UNKNOWN}
# A marketplace has a curator roster to vet. An individual curator IS the roster.
VETTING_RELEVANT = {PLATFORM}

CURATOR_SUBMISSIONS = "CURATOR_SUBMISSIONS"
PRESS_PR = "PRESS_PR"
PAID_MEDIA = "PAID_MEDIA"
CREATOR_PROMOTION = "CREATOR_PROMOTION"
ADVERTISING = "ADVERTISING"
RADIO = "RADIO"
DJ_POOL = "DJ_POOL"
CLUB = "CLUB"
SYNC = "SYNC"
SERVICE_TYPE_OTHER = "OTHER"

ALLOCATION_CATEGORIES = ["CURATOR_SUBMISSIONS", "PRESS_PR", "CREATOR_PROMOTION",
                         "DIGITAL_ADVERTISING", "RADIO_PROMOTION", "DJ_CLUB",
                         "SYNC_SUBMISSIONS", "OTHER"]

# The two vocabularies have to join somewhere; this is it.
SERVICE_TYPE_TO_ALLOCATION = {
    CURATOR_SUBMISSIONS: "CURATOR_SUBMISSIONS",
    PRESS_PR: "PRESS_PR",
    PAID_MEDIA: "PRESS_PR",
    CREATOR_PROMOTION: "CREATOR_PROMOTION",
    ADVERTISING: "DIGITAL_ADVERTISING",
    RADIO: "RADIO_PROMOTION",
    DJ_POOL: "DJ_CLUB",
    CLUB: "DJ_CLUB",
    SYNC: "SYNC_SUBMISSIONS",
}

def allocation_category_for(value):
    """Accept either vocabulary and return an allocation category.

    Cards hand this the service's own type (ADVERTISING, RADIO, SYNC…), which
    is not an allocation category; translating here means no call site can get
    it wrong.
    """
    if value in ALLOCATION_CATEGORIES:
        return value
    mapped = SERVICE_TYPE_TO_ALLOCATION.get(value)
    if mapped:
        return mapped
    if value in (None, "", SERVICE_TYPE_OTHER):
        return "OTHER"
    raise ValidationError(f"Unknown allocation category: {value}")


PLANNED = "PLANNED"
SUBMITTED = "SUBMITTED"
PAID = "PAID"
IN_REVIEW = "IN_REVIEW"
ACCEPTED = "ACCEPTED"
DECLINED = "DECLINED"
COMPLETED = "COMPLETED"
REFUND_REQUESTED = "REFUND_REQUESTED"
REFUNDED = "REFUNDED"
PLAN_STATUSES = [PLANNED, SUBMITTED, PAID, IN_REVIEW, ACCEPTED, DECLINED, COMPLETED,
                 REFUND_REQUESTED, REFUNDED]
# Payment is tracked orthogonally to review outcome: a declined pitch usually
# keeps the fee, so a paid item stays spent whatever the curator decided.
PLANNED_STATUSES = {PLANNED, SUBMITTED, IN_REVIEW, ACCEPTED}

# Verbatim copy the UI and the tests both depend on.
ENDORSEMENT_DISCLAIMER = (
    "REACH reviewed currently available public evidence about this service's business "
    "model and promotion practices. REACH Screened does not mean the service is endorsed "
    "by Spotify, Apple Music, Amazon Music, or another DSP."
)
CATEGORY_PROMISE = (
    "This area is for services where you pay for access, consideration, advertising, or "
    "professional promotion — never for guaranteed streams. REACH blocks services where "
    "it finds guarantee evidence; UNKNOWN never means safe."
)
LEAVING_REACH = "You are leaving REACH."
LEAVING_REACH_DETAIL = (
    "REACH screened this service using public evidence, but REACH does not control the "
    "provider or guarantee results."
)
ALLOCATION_BASIS = (
    "Split evenly across the {n} categories where REACH found at least one screened "
    "service with a computed campaign fit."
)
BLOCKED_STATEMENT = "REACH does not recommend this service."
BLOCKED_SUPPORT = (
    "Guaranteed or artificial streaming may expose a release to platform penalties and "
    "violates REACH promotion policy."
)
UNKNOWN_STATEMENT = (
    "REACH could not collect enough current evidence to assess this service."
)


# ---------------------------------------------------------------------------
# deterministic rule tables
# ---------------------------------------------------------------------------
#
# extractor.py's GUARANTEED_* classifications have no negation guard — "we do
# not guarantee placement" classifies as GUARANTEED_PLACEMENT there, which is
# right for scoring a submission page's risk and wrong for blocking a business.
# So extractor's verdict is corroboration only: it is re-validated through these
# guarded tables before it can count as a hard-block signal.

# (signal key, human label, pattern)
HARD_BLOCK_RULES = [
    ("guaranteed_streams", "Guaranteed streams",
     re.compile(r"guarantee\w*\s+(?:you\s+)?[\d,]*\s*\+?\s*"
                r"(?:real\s+|organic\s+|spotify\s+|apple\s+music\s+)*(?:streams|plays)\b", re.I)),
    ("guaranteed_streams", "Streams offered for sale",
     re.compile(r"\b(?:buy|purchase|order|get)\s+[\d,]+\s*\+?\s*"
                r"(?:real\s+|organic\s+|spotify\s+)*(?:streams|plays)\b", re.I)),
    ("stream_quantity_pricing", "Priced by stream quantity",
     re.compile(r"(?:\$|€|£)\s?[\d,]+(?:\.\d{2})?\s*(?:[-–—:]|for|=|→)\s*[\d,]+\s*\+?\s*"
                r"(?:streams|plays|saves|followers)\b", re.I)),
    ("guaranteed_placement", "Guaranteed playlist placement",
     re.compile(r"guarantee\w*\s+(?:playlist\s+|editorial\s+)?placement", re.I)),
    ("guaranteed_placement", "Guaranteed playlist placement",
     re.compile(r"placement\s+(?:is\s+)?guaranteed", re.I)),
    ("guaranteed_placement", "Guaranteed playlist adds",
     re.compile(r"guarantee\w*\s+(?:to\s+)?(?:be\s+)?add(?:ed|s)?\s+to\s+[^.]{0,40}playlists?",
                re.I)),
    ("guaranteed_saves", "Saves offered for sale",
     re.compile(r"\b(?:buy|purchase|order|guarantee\w*)\s+[\d,]*\s*\+?\s*"
                r"(?:spotify\s+)?saves\b", re.I)),
    ("guaranteed_followers", "Followers offered for sale",
     re.compile(r"\b(?:buy|purchase|order|guarantee\w*)\s+[\d,]*\s*\+?\s*"
                r"(?:spotify\s+|instagram\s+|tiktok\s+)?followers\b", re.I)),
    ("algorithmic_trigger", "Algorithmic triggering promised",
     re.compile(r"\b(?:trigger|guarantee\w*|unlock|hack|force)\s+(?:the\s+)?(?:spotify\s+)?"
                r"(?:discover\s+weekly|release\s+radar|algorithm)", re.I)),
    ("algorithmic_trigger", "Algorithmic triggering promised",
     re.compile(r"\b(?:discover\s+weekly|release\s+radar)\s+(?:in\s+\d+\s+\w+|guaranteed)", re.I)),
    ("bot_engagement", "Bot or artificial engagement",
     re.compile(r"\b(?:bots?|click\s*farms?|artificial\s+(?:streams|listeners|engagement|plays)|"
                r"fake\s+(?:streams|plays|listeners|followers))\b", re.I)),
    ("incentivized_streaming", "Undisclosed incentivized streaming",
     re.compile(r"\b(?:paid|incentivi[sz]ed)\s+listeners?\b|"
                r"\blisteners?\s+(?:are\s+)?paid\s+to\s+(?:stream|listen)", re.I)),
]

# Tested BEFORE the hard-block table and winning: a denial, a warning or a
# description of someone else's practice is not an offer.
_NEGATION_CUES = re.compile(
    r"\b(?:never|not|no|nor|don't|doesn't|do not|does not|didn't|won't|will not|cannot|"
    r"can't|without|avoid|beware|warning|scam|scams|refuse|refuses|reject|rejects|"
    r"prohibit\w*|ban\w*|unlike|instead of|rather than|steer clear|red flag|"
    r"other services|services that|anyone who|if a service|claims? to|promises? to|"
    r"so-called|allegedly|purport\w*)\b", re.I)
_NEGATION_WINDOW = 90

# The site's own offer voice: a guarantee only blocks when it sits alongside a
# price, a package or a buy/submit flow on the service's own page.
_OFFER_CONTEXT = re.compile(
    r"(?:\$|€|£)\s?\d|"
    r"\b(?:package|packages|pricing|price|plans?|per\s+track|per\s+song|per\s+release|"
    r"per\s+campaign|add\s+to\s+cart|checkout|buy\s+now|order\s+now|start\s+(?:your\s+)?"
    r"campaign|get\s+started|sign\s+up|submission\s+fee|submit\s+your)\b", re.I)

# A promotion_service row exists only when the page makes a first-party
# promotion offer. A page that merely writes ABOUT promotion services — a
# review, a listicle, an anti-scam article — is a source document and nothing
# more; there is no honest way to attribute one domain's text to another's
# business.
_OFFER_SIGNALS = [
    re.compile(r"\bsubmit\s+your\s+(?:music|track|song|demo|release|single)\b", re.I),
    re.compile(r"\bour\s+(?:packages?|pricing|service|services|platform|curators?|network|"
               r"team|writers?)\b", re.I),
    re.compile(r"\bwe\s+(?:offer|provide|promote|pitch|submit|review|accept|place|run)\b", re.I),
    re.compile(r"\b(?:pricing|packages?|plans?)\b[^.]{0,80}(?:\$|€|£)\s?\d", re.I),
    re.compile(r"(?:\$|€|£)\s?\d[\d,.]*\s*(?:per|/)\s*(?:track|song|release|campaign|"
               r"submission|month)", re.I),
    re.compile(r"\bsubmission\s+fee\b", re.I),
    re.compile(r"\b(?:start|launch)\s+(?:your\s+)?campaign\b", re.I),
]
_ARTICLE_SHAPE = [
    re.compile(r"\b(?:top|best|worst)\s+\d+\b", re.I),
    re.compile(r"\bin\s+this\s+(?:article|guide|post|review)\b", re.I),
    re.compile(r"\bhow\s+to\s+(?:spot|avoid|choose)\b", re.I),
    re.compile(r"\bwe\s+(?:reviewed|compared|tested|ranked)\b", re.I),
    re.compile(r"\b(?:reviewed|compared)\s+\d+\s+(?:services|platforms)\b", re.I),
]

# (field, value, label, pattern, guarded). First match per field wins, title
# before body. `guarded` is False for rules that are themselves denials — "we
# never use bots" is the claim, so running a negation guard over it would
# suppress exactly the fact it states.
FACT_RULES = [
    ("placement_discretionary", TRUE, "Placement stays with the curator",
     re.compile(r"\b(?:curators?|editors?|the\s+station)\s+(?:alone\s+)?decides?\b|"
                r"\bat\s+the\s+curator'?s?\s+discretion\b|"
                r"\b(?:does|do)\s+not\s+(?:buy|guarantee)\s+placement\b|"
                r"\bplacement\s+is\s+(?:never|not)\s+guaranteed\b|"
                r"\bno\s+guarantee\s+of\s+placement\b|"
                r"\bwe\s+(?:can|do)\s?n(?:o|')t\s+guarantee\s+(?:placement|coverage|a\s+feature)\b|"
                r"\bnot\s+(?:a\s+)?guarantee\w*\s+(?:of\s+)?placement\b", re.I), False),
    ("guaranteed_feedback", TRUE, "Guaranteed listen or reply",
     re.compile(r"\bguarantee\w*\s+(?:a\s+)?(?:listen|reply|response|feedback|review)\b|"
                r"\b(?:listen|reply|response|feedback)\s+(?:is\s+)?guaranteed\b|"
                r"\bwe\s+(?:reply|respond)\s+to\s+every\s+submission\b|"
                r"\bwritten\s+feedback\s+(?:on|for)\s+every\b", re.I), True),
    ("guaranteed_coverage", TRUE, "Coverage sold as a product",
     re.compile(r"\bguarantee\w*\s+(?:sponsored\s+)?(?:article|coverage|publication|feature|"
                r"interview|post)\b|\bsponsored\s+(?:article|post|content)\b|"
                r"\byour\s+article\s+will\s+be\s+published\b|\bpaid\s+(?:article|interview)\b",
                re.I), True),
    ("curator_vetting", TRUE, "Curators are vetted",
     re.compile(r"\b(?:we\s+)?vet\w*\s+(?:every\s+|each\s+|all\s+)?curators?\b|"
                r"\bcurators?\s+are\s+(?:vetted|screened|verified)\b|"
                r"\bcurator\s+vetting\b", re.I), True),
    ("playlist_vetting", TRUE, "Playlists are vetted",
     re.compile(r"\bplaylists?\s+are\s+(?:vetted|screened|checked|audited)\b|"
                r"\bplaylist\s+vetting\b|\bwe\s+(?:check|audit|remove)\s+playlists?\b", re.I), True),
    ("anti_bot_policy", TRUE, "States a no-bot policy",
     re.compile(r"\bno\s+bots?\b|\bnever\s+use\s+bots?\b|\bbot[-\s]free\b|"
                r"\bzero\s+tolerance\s+for\s+(?:artificial|fake|bot)\b|"
                r"\bwe\s+(?:do\s+not|never)\s+(?:use|sell|buy)\s+"
                r"(?:bots?|artificial|fake)\b", re.I), False),
    ("refund_policy", TRUE, "Refund terms published",
     re.compile(r"\brefund\s+(?:policy|terms)\b|\bwe\s+(?:will\s+)?refund\b|"
                r"\byou\s+(?:will|can)\s+be\s+refunded\b|\bmoney[-\s]back\b|"
                r"\brefunded\s+(?:if|when)\b", re.I), True),
    ("curator_compensation", TRUE, "Curators paid per placement",
     re.compile(r"\bcurators?\s+are\s+paid\s+(?:per|for)\s+(?:placement|adds?|playlisting)\b|"
                r"\bwe\s+pay\s+curators?\s+(?:per|for)\s+(?:placement|adds?)\b|"
                r"\bpaid\s+per\s+placement\b", re.I), True),
    ("curator_compensation", FALSE, "Curators are not paid for placement",
     re.compile(r"\bcurators?\s+are\s+(?:never|not)\s+paid\b|"
                r"\bwe\s+(?:do\s+not|never)\s+pay\s+curators?\b", re.I), False),
    ("artist_disclosure", TRUE, "Paid coverage is disclosed",
     re.compile(r"\b(?:always\s+)?(?:labell?ed|marked|disclosed)\s+as\s+sponsored\b|"
                r"\bclearly\s+disclosed\b", re.I), True),
]

# Category rules, ordered: first match wins, otherwise UNKNOWN.
CATEGORY_RULES = [
    (AD_PLATFORM, re.compile(r"\b(?:ad\s+platform|advertising\s+platform|"
                             r"(?:we\s+)?run\s+(?:your\s+)?ads?\b|ad\s+campaigns?|"
                             r"impressions|cost\s+per\s+click|cpm\b)", re.I)),
    (RADIO_PROMOTION, re.compile(r"\b(?:radio\s+promotion|radio\s+plugger|airplay\s+campaign|"
                                 r"college\s+radio\s+promotion|we\s+pitch\s+(?:your\s+)?"
                                 r"(?:music\s+)?to\s+radio)\b", re.I)),
    (PAID_MEDIA_OUTLET, re.compile(r"\b(?:sponsored\s+(?:article|post|content)|paid\s+"
                                   r"(?:article|editorial|interview)|advertorial|"
                                   r"our\s+magazine|our\s+publication)\b", re.I)),
    (PLATFORM, re.compile(r"\b(?:our\s+(?:network|roster|marketplace)\s+of\s+curators?|"
                          r"curator\s+(?:network|marketplace|roster)|"
                          r"hundreds\s+of\s+curators?|our\s+curators?\s+(?:will|review))\b",
                          re.I)),
    (INDIVIDUAL_CURATOR, re.compile(r"\b(?:i\s+(?:review|listen|curate)|my\s+playlists?|"
                                    r"i\s+am\s+a\s+curator|one\s+curator|"
                                    r"(?:we|i)\s+review\s+every\s+submission)\b", re.I)),
    (PROMOTION_COMPANY, re.compile(r"\b(?:music\s+pr|pr\s+agency|promotion\s+(?:company|agency)|"
                                   r"campaign\s+management|we\s+pitch\s+your\s+music|"
                                   r"press\s+campaign)\b", re.I)),
]

SERVICE_TYPE_RULES = [
    (CURATOR_SUBMISSIONS, re.compile(r"\b(?:playlist|curator)\b[^.]{0,40}\bsubmi\w+|"
                                     r"\bsubmit\s+your\s+(?:music|track|song)\b", re.I)),
    (PRESS_PR, re.compile(r"\b(?:press|pr\b|publicist|blog\s+coverage|media\s+campaign)\b", re.I)),
    (PAID_MEDIA, re.compile(r"\bsponsored\s+(?:article|post|content)|advertorial|"
                            r"paid\s+(?:article|interview)\b", re.I)),
    (CREATOR_PROMOTION, re.compile(r"\b(?:influencer|creator|tiktok\s+campaign|"
                                   r"content\s+creators?)\b", re.I)),
    (ADVERTISING, re.compile(r"\b(?:advertising|ad\s+campaigns?|impressions|banner\s+ads?)\b",
                             re.I)),
    (RADIO, re.compile(r"\b(?:radio|airplay|broadcast)\b", re.I)),
    (DJ_POOL, re.compile(r"\b(?:dj\s+pool|record\s+pool|promo\s+pool)\b", re.I)),
    (CLUB, re.compile(r"\b(?:club\s+promo|dance\s?floor|club\s+dj)\b", re.I)),
    (SYNC, re.compile(r"\b(?:sync\s+(?:licensing|submission|placement)|music\s+supervisor)\b",
                      re.I)),
]

_MONEY_RE = re.compile(
    r"(?:(\$|€|£)\s?(\d[\d,]*(?:\.\d{2})?)|(\d[\d,]*(?:\.\d{2})?)\s?(USD|EUR|GBP))", re.I)
_CURRENCY_BY_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP"}
_PRICING_CONTEXT = re.compile(
    r"\b(?:price|pricing|cost|costs|fee|fees|package|packages|plan|plans|per\s+track|"
    r"per\s+song|per\s+release|per\s+campaign|per\s+submission|from|starting\s+at|"
    r"starts?\s+at|submission\s+fee)\b", re.I)

# supports_field per stored fact. Signals share 'stream_guarantees' so the
# blocking evidence is one queryable set.
FIELD_SUPPORTS = {
    "placement_discretionary": "placement_policy",
    "guaranteed_feedback": "placement_policy",
    "guaranteed_coverage": "placement_policy",
    "curator_vetting": "curator_vetting",
    "playlist_vetting": "playlist_vetting",
    "curator_compensation": "curator_compensation",
    "anti_bot_policy": "anti_bot_policy",
    "refund_policy": "refund_policy",
    "artist_disclosure": "artist_disclosure",
}
TRISTATE_FIELDS = [
    "placement_discretionary", "guaranteed_feedback", "guaranteed_coverage",
    "guaranteed_playlist_placement", "guaranteed_streams", "guaranteed_followers",
    "guaranteed_saves", "anti_bot_policy", "curator_vetting", "playlist_vetting",
    "curator_compensation", "refund_policy",
]
# Hard-block signal -> the tri-state column it also settles.
SIGNAL_FIELDS = {
    "guaranteed_streams": "guaranteed_streams",
    "stream_quantity_pricing": "guaranteed_streams",
    "bot_engagement": "guaranteed_streams",
    "incentivized_streaming": "guaranteed_streams",
    "algorithmic_trigger": "guaranteed_streams",
    "guaranteed_placement": "guaranteed_playlist_placement",
    "guaranteed_saves": "guaranteed_saves",
    "guaranteed_followers": "guaranteed_followers",
}

# Reserved for a later phase. Phase One screens a service only on its own
# fetched pages: there is no honest mechanism yet for attributing domain A's
# text to domain B's business, and a random complaint is not proof. When
# third-party ingestion lands, packets must be tagged first-party claim vs
# third-party reporting vs REACH interpretation.
RESERVED_SUPPORTS = ("complaints_enforcement", "third_party_report")


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

def _excerpt_around(text, match, width=180):
    start = max(0, match.start() - width // 2)
    end = min(len(text), match.end() + width // 2)
    return (("…" if start > 0 else "")
            + " ".join(text[start:end].split())
            + ("…" if end < len(text) else ""))


def _negated(text, match):
    """Is this match a denial, a warning, or a description of someone else?

    The window is the current sentence only. A negation two sentences earlier
    says nothing about this clause: "we do not buy streams. Refund policy: …"
    must still register a refund policy.
    """
    start = max(0, match.start() - _NEGATION_WINDOW)
    window = text[start:match.start()]
    boundary = max(window.rfind(". "), window.rfind("! "), window.rfind("? "),
                   window.rfind("\n"))
    if boundary != -1:
        window = window[boundary + 1:]
    return bool(_NEGATION_CUES.search(window))


def has_offer_context(text):
    return bool(_OFFER_CONTEXT.search(text or ""))


def is_service_offer_page(sanitized):
    """First-party promotion offer, or a page writing about the industry?"""
    text = " ".join(filter(None, [sanitized.get("title") or "",
                                  sanitized.get("meta_description") or "",
                                  sanitized.get("visible_text") or ""]))
    if any(pattern.search(text) for pattern in _ARTICLE_SHAPE):
        return False
    return any(pattern.search(text) for pattern in _OFFER_SIGNALS)


def hard_block_signals(sanitized):
    """Guarded hard-block detection over one page's own words.

    A signal fires only when the guarantee language is unnegated AND the page
    carries the service's own offer context. Everything else — denials, warning
    articles, quoted scam copy — produces nothing.
    """
    text = " ".join(filter(None, [sanitized.get("title") or "",
                                  sanitized.get("meta_description") or "",
                                  sanitized.get("visible_text") or ""]))
    if not has_offer_context(text):
        return []
    found = {}
    for key, label, pattern in HARD_BLOCK_RULES:
        for match in pattern.finditer(text):
            if _negated(text, match):
                continue
            found.setdefault(key, {"signal": key, "label": label,
                                   "excerpt": _excerpt_around(text, match)})
            break
    return list(found.values())


def corroborating_classification(sanitized):
    """extractor's own verdict, re-validated here before it may count.

    extractor.classify has no negation guard by design — it scores a submission
    page's risk. Re-running the guarded tables is what turns it from a hint into
    a signal, so a page classified GUARANTEED_PLACEMENT on negated text cannot
    block anything.
    """
    verdict = extractor.classify(sanitized)
    if verdict["classification"] not in (extractor.GUARANTEED_STREAMS,
                                         extractor.GUARANTEED_PLACEMENT):
        return []
    return hard_block_signals(sanitized)


def extract_facts(sanitized):
    """Tri-state facts from one page, title tested before body."""
    title = sanitized.get("title") or ""
    body = " ".join(filter(None, [sanitized.get("meta_description") or "",
                                  sanitized.get("visible_text") or ""]))
    facts = {}
    for field, value, label, pattern, guarded in FACT_RULES:
        if field in facts:
            continue
        for text, confidence in ((title, 0.7), (body, 0.7)):
            match = pattern.search(text)
            if match is None:
                continue
            if guarded and _negated(text, match):
                continue
            facts[field] = {"field": field, "value": value, "label": label,
                            "excerpt": _excerpt_around(text, match),
                            "confidence": confidence}
            break
    return list(facts.values())


def _joined(sanitized):
    return " ".join(filter(None, [sanitized.get("title") or "",
                                  sanitized.get("meta_description") or "",
                                  sanitized.get("visible_text") or ""]))


def extract_category(sanitized, with_excerpt=False):
    text = _joined(sanitized)
    for category, pattern in CATEGORY_RULES:
        match = pattern.search(text)
        if match:
            return (category, _excerpt_around(text, match)) if with_excerpt else category
    return (CATEGORY_UNKNOWN, None) if with_excerpt else CATEGORY_UNKNOWN


def extract_service_types(sanitized, with_excerpt=False):
    text = _joined(sanitized)
    found, excerpt = [], None
    for name, pattern in SERVICE_TYPE_RULES:
        match = pattern.search(text)
        if match:
            found.append(name)
            excerpt = excerpt or _excerpt_around(text, match)
    return (found, excerpt) if with_excerpt else found


def _parse_money(match):
    symbol, symbol_amount, plain_amount, code = match.groups()
    raw = symbol_amount or plain_amount
    if raw is None:
        return None, None
    try:
        amount = float(raw.replace(",", ""))
    except ValueError:
        return None, None
    currency = _CURRENCY_BY_SYMBOL.get(symbol) if symbol else (code.upper() if code else None)
    return amount, currency


def extract_pricing(sanitized):
    """Amounts only from explicit page text in a pricing context.

    No price found is UNKNOWN — never zero, and never a guess from a package
    name.
    """
    text = sanitized.get("visible_text") or ""
    amounts, currency, excerpt = [], None, None
    for match in _MONEY_RE.finditer(text):
        window = text[max(0, match.start() - 90):min(len(text), match.end() + 90)]
        if not _PRICING_CONTEXT.search(window):
            continue
        amount, found_currency = _parse_money(match)
        if amount is None:
            continue
        amounts.append(amount)
        currency = currency or found_currency
        excerpt = excerpt or _excerpt_around(text, match)
    if not amounts:
        free = re.search(r"\b(?:free\s+to\s+submit|no\s+submission\s+fee|submissions?\s+are\s+free)\b",
                         text, re.I)
        if free:
            return {"model": extractor.FREE_SUBMISSION, "min": 0.0, "max": 0.0,
                    "currency": None, "excerpt": _excerpt_around(text, free)}
        return None
    return {"model": extractor.PAID_CONSIDERATION_COST, "min": min(amounts),
            "max": max(amounts), "currency": currency, "excerpt": excerpt}


_LOGIN_PRICING_CONTEXT = re.compile(
    r"\b(?:pricing|price|prices|cost|fee|fees|packages?|plans?|rates?)\b", re.I)


def pricing_login_wall(sanitized, url=None, pricing_url=None):
    """A login wall in front of a price, not merely a login wall.

    ``extractor.requires_login`` matches bare words like "dashboard", so a
    homepage mentioning a member area would otherwise be reported as the reason
    REACH could not read a price it never looked for.
    """
    if not extractor.requires_login(sanitized):
        return None
    text = sanitized.get("visible_text") or ""
    if url and pricing_url and url == pricing_url:
        match = re.search(r".", text)
        return _excerpt_around(text, match) if match else None
    match = _LOGIN_PRICING_CONTEXT.search(_joined(sanitized))
    if match is None:
        return None
    return _excerpt_around(_joined(sanitized), match)


def _same_domain_links(sanitized, domain):
    """Terms / privacy / pricing / submission / contact links the page publishes."""
    wanted = {
        "terms_url": re.compile(r"\bterms\b|\btos\b|\bconditions\b", re.I),
        "privacy_url": re.compile(r"\bprivacy\b", re.I),
        "pricing_url": re.compile(r"\bpricing\b|\bprices?\b|\bplans?\b", re.I),
        "submission_url": re.compile(r"\bsubmit\b|\bsubmission\b|\bstart\b", re.I),
        "contact_url": re.compile(r"\bcontact\b|\babout\b", re.I),
    }
    found = {}
    for link in sanitized.get("links", []):
        href = link.get("href") or ""
        if not href.lower().startswith(("http://", "https://")):
            continue
        host = urlsplit(href).hostname or ""
        if netguard.registrable_domain(host) != domain:
            continue
        haystack = f"{href} {link.get('text') or ''}"
        for field, pattern in wanted.items():
            if field not in found and pattern.search(haystack):
                found[field] = href
    return found


def business_model_summary(service):
    """Assembled from receipt-backed facts only, one clause per established fact.

    Deterministic template, no free prose: every sentence here is the rendering
    of a stored value that has a packet behind it.
    """
    parts = []
    category = service["category"]
    if category != CATEGORY_UNKNOWN:
        parts.append({
            PLATFORM: "A submission platform with a curator network.",
            PROMOTION_COMPANY: "A promotion company running campaigns on an artist's behalf.",
            INDIVIDUAL_CURATOR: "An individual curator reviewing submissions directly.",
            PAID_MEDIA_OUTLET: "A publication selling disclosed paid coverage.",
            AD_PLATFORM: "An advertising service selling ad delivery.",
            RADIO_PROMOTION: "A radio promotion service pitching to stations.",
        }[category])
    if service["pricing_model"] == extractor.FREE_SUBMISSION:
        parts.append("Submissions are free — no submission fee.")
    elif service["pricing_min"] is not None:
        currency = service["pricing_currency"] or ""
        if service["pricing_min"] == service["pricing_max"]:
            parts.append(f"Published price: {service['pricing_min']:g} {currency}".strip() + ".")
        else:
            parts.append(
                f"Published prices run {service['pricing_min']:g}–{service['pricing_max']:g} "
                f"{currency}".strip() + ".")
    if service["placement_discretionary"] == TRUE:
        parts.append("Placement stays at the curator's discretion.")
    if service["guaranteed_feedback"] == TRUE:
        parts.append("A listen or written reply is guaranteed.")
    if service["guaranteed_coverage"] == TRUE:
        parts.append("Coverage itself is sold as the product.")
    return " ".join(parts) or None


# ---------------------------------------------------------------------------
# entity resolution
# ---------------------------------------------------------------------------

def get_service(service_id):
    return db.query_one("SELECT * FROM promotion_service WHERE id = ?", (service_id,))


def canonical_service(service_id):
    """Follow a merge chain to the surviving row."""
    seen = set()
    current = get_service(service_id)
    while current is not None and current["canonical_id"] and current["id"] not in seen:
        seen.add(current["id"])
        current = get_service(current["canonical_id"])
    return current


def service_by_domain(domain, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    row = db.query_one(
        "SELECT * FROM promotion_service WHERE tenant_id = ? AND canonical_domain = ?",
        (tenant_id, domain),
    )
    return canonical_service(row["id"]) if row is not None else None


def ensure_service(name, domain, url, tenant_id=None):
    """Insert or find the row for this domain, following any merge chain, so a
    post-merge sweep accumulates on the winner rather than reviving the loser."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    existing = service_by_domain(domain, tenant_id)
    if existing is not None:
        return existing["id"]
    now = clock.now_iso()
    service_id = db.new_id("promo")
    db.insert("promotion_service", {
        "id": service_id,
        "tenant_id": tenant_id,
        "name": name or domain,
        "canonical_domain": domain,
        "url": url,
        "company_name": None,
        "category": CATEGORY_UNKNOWN,
        "service_types_json": json.dumps([]),
        "supported_channels_json": json.dumps([]),
        "supported_genres_json": json.dumps([]),
        "supported_territories_json": json.dumps([]),
        "pricing_model": extractor.COST_UNKNOWN,
        "pricing_min": None, "pricing_max": None, "pricing_currency": None,
        "pricing_last_verified_at": None,
        "business_model_summary": None,
        "placement_discretionary": TRISTATE_UNKNOWN,
        "guaranteed_feedback": TRISTATE_UNKNOWN,
        "guaranteed_coverage": TRISTATE_UNKNOWN,
        "guaranteed_playlist_placement": TRISTATE_UNKNOWN,
        "guaranteed_streams": TRISTATE_UNKNOWN,
        "guaranteed_followers": TRISTATE_UNKNOWN,
        "guaranteed_saves": TRISTATE_UNKNOWN,
        "anti_bot_policy": TRISTATE_UNKNOWN,
        "curator_vetting": TRISTATE_UNKNOWN,
        "playlist_vetting": TRISTATE_UNKNOWN,
        "curator_compensation": TRISTATE_UNKNOWN,
        "refund_policy": TRISTATE_UNKNOWN,
        "terms_url": None, "privacy_url": None, "pricing_url": None,
        "submission_url": None, "contact_url": None,
        "screening_status": UNKNOWN,
        "block_reason": None,
        "last_screened_at": None,
        "next_review_at": None,
        "manual_review_required": 0,
        "notes": None,
        "commercial_relationship_type": "NONE",
        "commercial_disclosure": None,
        "canonical_id": None,
        "created_at": now,
        "updated_at": now,
    })
    audit.record("promotion.discovered", entity_type="promotion_service",
                 entity_id=service_id, payload={"domain": domain, "name": name or domain})
    return service_id


def services(tenant_id=None, limit=500):
    """Every surviving service row. Merge losers never appear in a listing."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    return db.query(
        "SELECT * FROM promotion_service WHERE tenant_id = ? AND canonical_id IS NULL "
        "ORDER BY name COLLATE NOCASE LIMIT ?",
        (tenant_id, limit),
    )


# ---------------------------------------------------------------------------
# evidence: current packets and fact recomputation
# ---------------------------------------------------------------------------

def current_packets(service_id):
    """The most recent read of each URL backing this service.

    Evidence is append-only, so a page that dropped a claim still has its old
    packet. Screening reads only the latest retrieval per URL: that is what
    makes a removed guarantee stop blocking, and a newly added one start.
    """
    # Ordered by rowid, so "newest" is the last row written for a URL rather
    # than whatever a same-second timestamp tie happens to return first.
    rows = db.query(
        "SELECT * FROM evidence_packet WHERE entity_type = ? AND entity_id = ? "
        "ORDER BY rowid",
        ("promotion_service", service_id),
    )
    # Timestamps are second-granular, so a rescan seconds after the first read
    # would leave both batches looking current. Every packet from one fetch
    # carries that fetch's content hash, which is the batch identifier: the
    # current batch is the newest packet's hash for that URL.
    newest = {}
    for row in rows:
        newest[row["source_url"]] = (row["content_hash"], row["retrieved_at"] or "")
    current = []
    for row in rows:
        entry = newest.get(row["source_url"])
        if entry is None:
            continue
        content_hash, retrieved_at = entry
        if (row["retrieved_at"] or "") != retrieved_at:
            continue
        if content_hash is not None and row["content_hash"] != content_hash:
            continue
        current.append(row)
    return current


def _packet_value(row):
    try:
        return json.loads(row["extracted_value_json"]) if row["extracted_value_json"] else None
    except (TypeError, ValueError):
        return None


def signals_from(packets):
    """Hard-block signals backed by the service's own current pages."""
    found = {}
    for row in packets:
        if row["supports_field"] != "stream_guarantees":
            continue
        value = _packet_value(row) or {}
        key = value.get("signal")
        if not key:
            continue
        found.setdefault(key, {"signal": key, "label": value.get("label") or key,
                               "excerpt": row["excerpt"], "source_url": row["source_url"],
                               "evidence_id": row["id"]})
    return list(found.values())


def recompute_facts(service_id):
    """Derive every tri-state from the current packets.

    Conflict rule: two current packets from the service's own pages supporting
    opposite definite values at equal confidence leave the field UNKNOWN with a
    stated reason, keep both receipts, and raise the review flag. A flip needs
    strictly better confidence — or the original page saying something new.
    """
    service = get_service(service_id)
    if service is None:
        return {}
    packets = current_packets(service_id)

    claims = {}
    for row in packets:
        value = _packet_value(row) or {}
        field = value.get("field")
        if field:
            claims.setdefault(field, []).append((value.get("value"), row["confidence"], row))
    for signal in signals_from(packets):
        field = SIGNAL_FIELDS.get(signal["signal"])
        if field:
            claims.setdefault(field, []).append((TRUE, 0.9, None))

    payload, conflicts = {}, []
    for field in TRISTATE_FIELDS:
        entries = claims.get(field) or []
        if not entries:
            payload[field] = TRISTATE_UNKNOWN
            continue
        best_true = max((c for value, c, _ in entries if value == TRUE), default=None)
        best_false = max((c for value, c, _ in entries if value == FALSE), default=None)
        if best_true is not None and best_false is not None:
            if best_true == best_false:
                payload[field] = TRISTATE_UNKNOWN
                conflicts.append(field)
                continue
            payload[field] = TRUE if best_true > best_false else FALSE
        elif best_true is not None:
            payload[field] = TRUE
        else:
            payload[field] = FALSE

    # Category, service types, genres and pricing are derived the same way the
    # tri-states are: from the current read of each page. Write-once category
    # and a forever-growing type union would keep asserting things no current
    # page supports.
    categories, types, genres = [], set(), set()
    price_mins, price_maxes, currencies, models, priced_at = [], [], [], [], []
    for row in packets:
        value = _packet_value(row) or {}
        if row["supports_field"] == "business_model":
            if value.get("category") and value["category"] != CATEGORY_UNKNOWN:
                categories.append(value["category"])
            types.update(value.get("service_types") or [])
            genres.update(value.get("genres") or [])
        elif row["supports_field"] == "pricing" and value.get("min") is not None:
            price_mins.append(value["min"])
            price_maxes.append(value.get("max", value["min"]))
            if value.get("currency"):
                currencies.append(value["currency"])
            if value.get("model"):
                models.append(value["model"])
            priced_at.append(row["retrieved_at"])

    payload["category"] = categories[0] if categories else CATEGORY_UNKNOWN
    payload["service_types_json"] = json.dumps(sorted(types))
    payload["supported_genres_json"] = json.dumps(sorted(genres))
    if price_mins:
        payload.update({
            "pricing_model": models[0] if models else extractor.COST_UNKNOWN,
            "pricing_min": min(price_mins),
            "pricing_max": max(price_maxes),
            "pricing_currency": currencies[0] if currencies else None,
            "pricing_last_verified_at": max(priced_at),
        })
    else:
        # No current page states a price any more: the old number is not news.
        payload.update({"pricing_model": extractor.COST_UNKNOWN, "pricing_min": None,
                        "pricing_max": None, "pricing_currency": None,
                        "pricing_last_verified_at": None})

    if conflicts:
        payload["manual_review_required"] = 1
    payload["updated_at"] = clock.now_iso()
    db.update("promotion_service", service_id, payload)
    if conflicts:
        audit.record("promotion.conflicting_evidence", entity_type="promotion_service",
                     entity_id=service_id, payload={"fields": sorted(conflicts)})
    refreshed = get_service(service_id)
    summary = business_model_summary(refreshed)
    if summary != refreshed["business_model_summary"]:
        db.update("promotion_service", service_id, {"business_model_summary": summary})
    return {"conflicts": conflicts}


# ---------------------------------------------------------------------------
# screening
# ---------------------------------------------------------------------------

def rescreen_window(status):
    return {
        SCREENED: RESCREEN_DAYS_SCREENED,
        CAUTION: RESCREEN_DAYS_CAUTION,
        BLOCKED: RESCREEN_DAYS_BLOCKED,
    }.get(status, RESCREEN_DAYS_UNKNOWN)


def page_ok_count(service, tenant_id=None):
    tenant_id = tenant_id or service["tenant_id"]
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM source_document WHERE tenant_id = ? AND domain = ? "
        "AND fetch_status = ?",
        (tenant_id, service["canonical_domain"], evidence.FETCH_OK),
    )
    return row["n"] if row else 0


def screening_inputs(service):
    """The evidence-derived projection screening runs on.

    Commercial relationship and disclosure are absent from this dict by
    construction, not by discipline: the screening function is handed this and
    never the row, so an affiliate arrangement cannot reach the decision even by
    accident.
    """
    window = rescreen_window(service["screening_status"])
    packets = current_packets(service["id"])
    freshness = {}
    for row in packets:
        value = _packet_value(row) or {}
        field = value.get("field") or row["supports_field"]
        days = clock.days_since(row["retrieved_at"])
        fresh = days is not None and days <= window
        freshness[field] = freshness.get(field, True) and fresh

    facts = {
        "id": service["id"],
        "name": service["name"],
        "canonical_domain": service["canonical_domain"],
        "company_name": service["company_name"],
        "category": service["category"],
        "service_types": json.loads(service["service_types_json"] or "[]"),
        "pricing_model": service["pricing_model"],
        "pricing_min": service["pricing_min"],
        "pricing_max": service["pricing_max"],
        "pricing_currency": service["pricing_currency"],
        "pricing_last_verified_at": service["pricing_last_verified_at"],
        "business_model_summary": service["business_model_summary"],
        "terms_url": service["terms_url"],
        "contact_url": service["contact_url"],
        "submission_url": service["submission_url"],
        "pricing_url": service["pricing_url"],
        "page_ok_count": page_ok_count(service),
        "manual_review_required": bool(service["manual_review_required"]),
        "fact_freshness": freshness,
        "window_days": window,
        "pricing_login_walled": any(
            (_packet_value(row) or {}).get("login_walled") for row in packets),
    }
    for field in TRISTATE_FIELDS:
        facts[field] = service[field]
    return facts


def _policy_evidence(tenant_id):
    return db.query(
        "SELECT * FROM evidence_packet WHERE tenant_id = ? AND entity_type = ? "
        "ORDER BY retrieved_at DESC",
        (tenant_id, "promotion_policy"),
    )


def screen_service(facts, evidence_rows, policy_rows=()):
    """The deterministic ladder. Starts at UNKNOWN and only moves on evidence.

    BLOCKED always wins. CAUTION and SCREENED are reachable only when their full
    conditions hold — a service that fails a SCREENED condition without a higher
    rung firing stays UNKNOWN, and every unmet criterion is named.
    """
    signals = signals_from(evidence_rows)
    reasons = []
    fresh = facts.get("fact_freshness") or {}

    if signals:
        labels = sorted({signal["label"] for signal in signals})
        block_reason = (
            f"{'; '.join(labels)} — found in this service's own offer pages."
        )
        return {
            "status": BLOCKED,
            "score": _score(facts, signals),
            "components": _components(facts, signals),
            "signals": signals,
            "reasons": [{"sign": "-", "text": label} for label in labels],
            "block_reason": block_reason,
            "version": PROMO_VERSION,
        }

    caution = []
    if facts.get("guaranteed_coverage") == TRUE:
        caution.append("Coverage is sold as a product — paid media needs disclosure clarity")
    if facts.get("curator_compensation") == TRUE:
        caution.append("Curators are paid per placement")
    if facts.get("refund_policy") == TRISTATE_UNKNOWN and facts.get("pricing_min") is not None:
        caution.append("No refund policy found while pricing is published")
    if facts.get("category") in VETTING_RELEVANT:
        if facts.get("curator_vetting") == TRISTATE_UNKNOWN:
            caution.append("Curator vetting not evidenced for a curator marketplace")
        if facts.get("playlist_vetting") == TRISTATE_UNKNOWN:
            caution.append("Playlist vetting not evidenced for a curator marketplace")
    if facts.get("manual_review_required"):
        caution.append("Flagged for manual review")
    stale_policy = [row for row in policy_rows if evidence.is_stale(row)]
    if policy_rows and stale_policy and facts.get("category") in PLACEMENT_RELEVANT:
        caution.append("Platform policy evidence is out of date — compatibility uncertain")

    if caution:
        return {
            "status": CAUTION,
            "score": _score(facts, signals),
            "components": _components(facts, signals),
            "signals": signals,
            "reasons": [{"sign": "?", "text": text} for text in caution],
            "block_reason": None,
            "version": PROMO_VERSION,
        }

    unmet = []
    if facts.get("category") in PLACEMENT_RELEVANT and facts.get("placement_discretionary") != TRUE:
        unmet.append("placement discretion not evidenced")
    pricing_known = (facts.get("pricing_min") is not None
                     or facts.get("pricing_model") == extractor.FREE_SUBMISSION)
    if not pricing_known:
        unmet.append("pricing is behind a login — REACH does not read login-walled pages"
                     if facts.get("pricing_login_walled") else "pricing unavailable")
    if not facts.get("business_model_summary"):
        unmet.append("business model not established from evidence")
    if not (facts.get("name") and facts.get("canonical_domain")
            and (facts.get("terms_url") or facts.get("contact_url"))):
        unmet.append("no terms or contact page found")
    if facts.get("page_ok_count", 0) < 2:
        unmet.append("fewer than two pages of this service could be read")
    if any(value is False for value in fresh.values()):
        unmet.append("evidence stale")

    if unmet:
        reasons = [{"sign": "?", "text": text} for text in unmet]
        return {
            "status": UNKNOWN,
            "score": _score(facts, signals),
            "components": _components(facts, signals),
            "signals": signals,
            "reasons": reasons,
            "block_reason": None,
            "version": PROMO_VERSION,
        }

    met = ["No guarantee evidence found in this service's own pages",
           "Placement stays with the curator" if facts.get("placement_discretionary") == TRUE
           else "Placement discretion not applicable to this category",
           "Pricing is published",
           "Business model assembled from evidence",
           "Company identity and terms or contact page found"]
    return {
        "status": SCREENED,
        "score": _score(facts, signals),
        "components": _components(facts, signals),
        "signals": signals,
        "reasons": [{"sign": "+", "text": text} for text in met],
        "block_reason": None,
        "version": PROMO_VERSION,
    }


COMPONENT_WEIGHTS = {
    "company_identity": 0.12,
    "business_model_transparency": 0.12,
    "pricing_transparency": 0.12,
    "placement_independence": 0.16,
    "anti_bot_policy": 0.08,
    "curator_vetting": 0.06,
    "playlist_vetting": 0.05,
    "terms_clarity": 0.07,
    "refund_clarity": 0.06,
    "evidence_freshness": 0.06,
    "policy_compatibility": 0.05,
    "risk_signals": 0.05,
}

COMPONENT_LABELS = {
    "company_identity": "Company identity published",
    "business_model_transparency": "Business model stated",
    "pricing_transparency": "Pricing published",
    "placement_independence": "Placement stays discretionary",
    "anti_bot_policy": "Anti-bot policy",
    "curator_vetting": "Curator vetting",
    "playlist_vetting": "Playlist vetting",
    "terms_clarity": "Terms published",
    "refund_clarity": "Refund policy",
    "evidence_freshness": "Evidence freshness",
    "policy_compatibility": "Platform policy compatibility",
    "risk_signals": "No guarantee signals",
}


def _tri(value):
    """Tri-state to component value: UNKNOWN stays None, never 0."""
    if value == TRUE:
        return 1.0
    if value == FALSE:
        return 0.0
    return None


def _components(facts, signals):
    fresh = facts.get("fact_freshness") or {}
    components = {
        "company_identity": 1.0 if (facts.get("company_name") or facts.get("name")) and (
            facts.get("terms_url") or facts.get("contact_url")) else None,
        "business_model_transparency": 1.0 if facts.get("business_model_summary") else None,
        "pricing_transparency": (
            1.0 if facts.get("pricing_min") is not None
            or facts.get("pricing_model") == extractor.FREE_SUBMISSION else None),
        "placement_independence": _tri(facts.get("placement_discretionary")),
        "anti_bot_policy": _tri(facts.get("anti_bot_policy")),
        "curator_vetting": _tri(facts.get("curator_vetting")),
        "playlist_vetting": _tri(facts.get("playlist_vetting")),
        "terms_clarity": 1.0 if facts.get("terms_url") else None,
        "refund_clarity": _tri(facts.get("refund_policy")),
        "evidence_freshness": (None if not fresh
                               else (1.0 if all(fresh.values()) else 0.0)),
        "policy_compatibility": None,
        "risk_signals": 0.0 if signals else None,
    }
    return components


# A renormalized average over one known component is not a score, it is that
# component wearing a percent sign. Below this share of the total weight REACH
# has not measured enough to publish a number at all.
MIN_SCORE_COVERAGE = 0.5

# Freshness says when REACH last read the pages, not what they said. On its own
# it can never carry a score.
NON_SUBSTANTIVE_COMPONENTS = {"evidence_freshness"}


def score_coverage(components):
    """Share of the total component weight that is actually known."""
    return sum(COMPONENT_WEIGHTS[key] for key, value in components.items()
               if value is not None)


def _score(facts, signals):
    """Weighted over known components, or None when too little is known.

    The status is the user-facing truth; this number only ranks services that
    were measured comparably. UNKNOWN never renormalizes its way to 100.
    """
    components = _components(facts, signals)
    known = {key: value for key, value in components.items() if value is not None}
    if not known:
        return None
    if not set(known) - NON_SUBSTANTIVE_COMPONENTS:
        return None
    weight_sum = sum(COMPONENT_WEIGHTS[key] for key in known)
    if weight_sum < MIN_SCORE_COVERAGE:
        return None
    weighted = sum(COMPONENT_WEIGHTS[key] * value for key, value in known.items())
    return round(weighted / weight_sum * 100) if weight_sum else None


def latest_screening(service_id):
    return db.query_one(
        "SELECT * FROM screening_result WHERE service_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (service_id,),
    )


def latest_override(service_id):
    return db.query_one(
        "SELECT * FROM screening_result WHERE service_id = ? AND human_override = 1 "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (service_id,),
    )


def screening_history(service_id, limit=20):
    rows = db.query(
        "SELECT * FROM screening_result WHERE service_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (service_id, limit),
    )
    return [_with_coverage(row) for row in rows]


def _with_coverage(row):
    """A score is meaningless without how much of the service was measured."""
    item = dict(row)
    try:
        components = json.loads(row["components_json"] or "{}")
    except (TypeError, ValueError):
        components = {}
    item["components"] = components
    item["coverage"] = round(score_coverage(components) * 100) if components else 0
    return item


def latest_automated_screening(service_id):
    return db.query_one(
        "SELECT * FROM screening_result WHERE service_id = ? AND human_override = 0 "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (service_id, 1),
    )


def effective_screening(service_id):
    """The screening row that actually produced the service's current status.

    After an override plus a later automated pass, the newest row is not the
    one in force — showing its reasons under the effective badge would have the
    dossier explain a BLOCK with "no guarantee evidence found".
    """
    service = get_service(service_id)
    if service is None:
        return None
    latest = latest_screening(service_id)
    if latest is not None and latest["status"] == service["screening_status"]:
        return latest
    for row in db.query(
        "SELECT * FROM screening_result WHERE service_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 20", (service_id,)
    ):
        if row["status"] == service["screening_status"]:
            return row
    return latest


def _worse(current, candidate):
    return candidate if _SEVERITY[candidate] > _SEVERITY[current] else current


def run_screening(service_id):
    """One automated pass: recompute facts, run the ladder, persist, audit."""
    service = get_service(service_id)
    if service is None:
        return None
    recompute_facts(service_id)
    service = get_service(service_id)

    facts = screening_inputs(service)
    packets = current_packets(service_id)
    result = screen_service(facts, packets, _policy_evidence(service["tenant_id"]))

    now = clock.now_iso()
    db.insert("screening_result", {
        "id": db.new_id("scr"),
        "tenant_id": service["tenant_id"],
        "service_id": service_id,
        "status": result["status"],
        "score": result["score"],
        "components_json": json.dumps(result["components"]),
        "signals_json": json.dumps(result["signals"]),
        "reasons_json": json.dumps(result["reasons"]),
        "block_reason": result["block_reason"],
        "human_override": 0,
        "override_reason": None,
        "version": result["version"],
        "created_at": now,
    })

    # An automated pass may TIGHTEN past a human override; it may never loosen
    # one. Lifting a human decision takes another explicit human decision.
    override = latest_override(service_id)
    effective, block_reason = result["status"], result["block_reason"]
    if override is not None:
        effective = _worse(result["status"], override["status"])
        if effective == override["status"] and effective != result["status"]:
            block_reason = override["block_reason"]

    previous = service["screening_status"]
    db.update("promotion_service", service_id, {
        "screening_status": effective,
        "block_reason": block_reason,
        "last_screened_at": now,
        "next_review_at": clock.days_from_now(rescreen_window(effective)),
        "updated_at": now,
    })
    audit.record("promotion.screened", entity_type="promotion_service", entity_id=service_id,
                 payload={"status": effective, "automated_status": result["status"]})
    if effective == BLOCKED and previous != BLOCKED:
        audit.record("promotion.blocked", entity_type="promotion_service",
                     entity_id=service_id, payload={"reason": block_reason})
    return {**result, "effective_status": effective}


def override_screening(service_id, status, reason):
    """A human decision, protected from automated reversal."""
    rbac.require("promotion.screen")
    if status not in SCREENING_STATUSES:
        raise ValidationError(f"Unknown screening status: {status}")
    if not reason or not str(reason).strip():
        raise ValidationError("An override requires a reason")
    service = get_service(service_id)
    if service is None:
        raise ValidationError("Unknown promotion service")

    now = clock.now_iso()
    db.insert("screening_result", {
        "id": db.new_id("scr"),
        "tenant_id": service["tenant_id"],
        "service_id": service_id,
        "status": status,
        "score": None,
        "components_json": json.dumps({}),
        "signals_json": json.dumps([]),
        "reasons_json": json.dumps([{"sign": "!", "text": f"Manual override: {reason}"}]),
        "block_reason": f"Manual override: {reason}" if status == BLOCKED else None,
        "human_override": 1,
        "override_reason": str(reason).strip(),
        "version": PROMO_VERSION,
        "created_at": now,
    })
    # Escalating to CAUTION asks for a review; deciding clears the request.
    db.update("promotion_service", service_id, {
        "screening_status": status,
        "block_reason": f"Manual override: {reason}" if status == BLOCKED else None,
        "next_review_at": clock.days_from_now(rescreen_window(status)),
        "manual_review_required": 1 if status == CAUTION else 0,
        "updated_at": now,
    })
    audit.record("promotion.overridden", entity_type="promotion_service", entity_id=service_id,
                 payload={"status": status, "reason": str(reason).strip()},
                 actor_kind=audit.ACTOR_USER)
    return status


def resolve_review(service_id):
    """Clear the review flag without touching the status. Reviews resolve."""
    rbac.require("promotion.screen")
    service = get_service(service_id)
    if service is None:
        raise ValidationError("Unknown promotion service")
    db.update("promotion_service", service_id, {"manual_review_required": 0,
                                                "updated_at": clock.now_iso()})
    audit.record("promotion.review_resolved", entity_type="promotion_service",
                 entity_id=service_id, payload={}, actor_kind=audit.ACTOR_USER)
    return True


def set_commercial_relationship(service_id, relationship_type, disclosure=None):
    """Operator-managed: REACH has no way to discover its own arrangements.

    Screening cannot see either field — see screening_inputs — so recording one
    changes what the UI discloses, never what the ladder decides.
    """
    rbac.require("promotion.screen")
    if relationship_type not in ("NONE", "AFFILIATE", "SPONSORED", "PARTNER"):
        raise ValidationError(f"Unknown commercial relationship: {relationship_type}")
    if relationship_type != "NONE" and not (disclosure or "").strip():
        raise ValidationError("A commercial relationship requires a disclosure to show")
    db.update("promotion_service", service_id, {
        "commercial_relationship_type": relationship_type,
        "commercial_disclosure": (disclosure or "").strip() or None,
        "updated_at": clock.now_iso(),
    })
    audit.record("promotion.commercial_relationship", entity_type="promotion_service",
                 entity_id=service_id, payload={"type": relationship_type},
                 actor_kind=audit.ACTOR_USER)
    return relationship_type


def set_note(service_id, text):
    rbac.require("promotion.screen")
    db.update("promotion_service", service_id, {"notes": (text or "")[:2000],
                                                "updated_at": clock.now_iso()})
    audit.record("promotion.note", entity_type="promotion_service", entity_id=service_id,
                 payload={}, actor_kind=audit.ACTOR_USER)
    return True


def is_stale(service):
    """Past its rescreen window — the screening itself is out of date."""
    if not service["next_review_at"]:
        return service["last_screened_at"] is None
    return clock.is_past(service["next_review_at"])


def effective_override(service):
    """The override the current status came from, or None."""
    override = latest_override(service["id"])
    if override is None or override["status"] != service["screening_status"]:
        return None
    latest = latest_screening(service["id"])
    if latest is not None and latest["human_override"] == 0:
        # An automated pass ran later; the override still holds only when it is
        # at least as restrictive as the automated verdict.
        if _SEVERITY[override["status"]] < _SEVERITY[latest["status"]]:
            return None
    return override


# ---------------------------------------------------------------------------
# campaign fit — a separate axis from safety
# ---------------------------------------------------------------------------

FIT_WEIGHTS = {
    "genre_overlap": 0.30,
    "territory_overlap": 0.15,
    "language_match": 0.10,
    "channel_match": 0.20,
    "goal_match": 0.10,
    "budget_fit": 0.15,
}

FIT_LABELS = {
    "genre_overlap": "Genre overlap",
    "territory_overlap": "Territory overlap",
    "language_match": "Language match",
    "channel_match": "Channel match",
    "goal_match": "Campaign goal match",
    "budget_fit": "Fits the promotion budget",
}


def _overlap(left, right):
    """UNKNOWN, not zero, when either side is empty."""
    left_set = {str(item).strip().lower() for item in (left or []) if str(item).strip()}
    right_set = {str(item).strip().lower() for item in (right or []) if str(item).strip()}
    if not left_set or not right_set:
        return None
    direct = left_set & right_set
    if direct:
        return min(1.0, len(direct) / min(len(left_set), 3))
    for item in left_set:
        for other in right_set:
            if item in other or other in item:
                return 0.5
    return 0.0


def fit_for_campaign(service, campaign, profile_values):
    settings = campaigns.settings(campaign["id"]) or {}
    genres = [profile_values.get("primary_genre")] + list(
        profile_values.get("secondary_genres") or []) + list(
        profile_values.get("microgenres") or [])
    service_genres = json.loads(service["supported_genres_json"] or "[]")
    service_territories = json.loads(service["supported_territories_json"] or "[]")
    service_types = json.loads(service["service_types_json"] or "[]")

    components = {
        "genre_overlap": _overlap([g for g in genres if g], service_genres),
        "territory_overlap": _overlap(settings.get("territories"), service_territories),
        "language_match": None,
        "channel_match": _overlap(settings.get("channels"),
                                  [_channel_for(t) for t in service_types]),
        "goal_match": _overlap(settings.get("priorities"), service_types),
        "budget_fit": _budget_fit(service, campaign),
    }
    language = profile_values.get("language")
    service_languages = json.loads(service["supported_channels_json"] or "[]")
    if language and service_languages:
        components["language_match"] = _overlap([language], service_languages)

    known = {key: value for key, value in components.items() if value is not None}
    score = None
    if known:
        weight_sum = sum(FIT_WEIGHTS[key] for key in known)
        weighted = sum(FIT_WEIGHTS[key] * value for key, value in known.items())
        score = round(weighted / weight_sum * 100) if weight_sum else None

    reasons = []
    for key, value in sorted(components.items(), key=lambda pair: -FIT_WEIGHTS[pair[0]]):
        if value is None:
            reasons.append({"sign": "?", "text": f"{FIT_LABELS[key]} unverified"})
        elif value >= 0.75:
            reasons.append({"sign": "+", "text": FIT_LABELS[key]})
    return {"score": score, "components": components, "reasons": reasons[:6],
            "version": FIT_VERSION}


def _channel_for(service_type):
    return {
        CURATOR_SUBMISSIONS: "PLAYLIST", PRESS_PR: "BLOG", PAID_MEDIA: "PUBLICATION",
        RADIO: "RADIO", DJ_POOL: "DJ_POOL", CLUB: "DJ", CREATOR_PROMOTION: "CREATOR",
    }.get(service_type, service_type)


def _budget_fit(service, campaign):
    budget = campaign["promotion_budget_amount"]
    if budget is None or service["pricing_min"] is None:
        return None
    if service["pricing_min"] <= budget:
        return 1.0
    return 0.0


def persist_fit(service_id, campaign_id, result, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    fit_id = db.new_id("sfit")
    db.insert("service_fit_score", {
        "id": fit_id,
        "tenant_id": tenant_id,
        "service_id": service_id,
        "campaign_id": campaign_id,
        "score": result["score"],
        "components_json": json.dumps(result["components"]),
        "reasons_json": json.dumps(result["reasons"]),
        "version": result["version"],
        "created_at": clock.now_iso(),
    })
    return fit_id


def latest_fit(service_id, campaign_id):
    return db.query_one(
        "SELECT * FROM service_fit_score WHERE service_id = ? AND campaign_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (service_id, campaign_id),
    )


def compute_fits(campaign_id, only_missing=False):
    """Fit for every surviving, non-dismissed service. Never for a BLOCKED one:
    safety and fit are separate axes and a blocked service has no fit to rank.

    ``only_missing`` is what a page load uses — the table is append-only, so a
    GET must not add a row per visit.
    """
    campaign = campaigns.get(campaign_id)
    if campaign is None:
        return []
    profile_id = profile.get_or_create(campaign["recording_id"])
    values = profile.values(profile_id)
    dismissed = dismissed_ids(campaign_id)
    computed = []
    for service in services(campaign["tenant_id"]):
        if service["screening_status"] == BLOCKED or service["id"] in dismissed:
            continue
        if only_missing and latest_fit(service["id"], campaign_id) is not None:
            continue
        result = fit_for_campaign(service, campaign, values)
        persist_fit(service["id"], campaign_id, result, campaign["tenant_id"])
        computed.append(service["id"])
    return computed


# ---------------------------------------------------------------------------
# sweep state
# ---------------------------------------------------------------------------

def state(tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    row = db.query_one("SELECT * FROM promotion_state WHERE tenant_id = ?", (tenant_id,))
    if row is None:
        return {"tenant_id": tenant_id, "last_sweep_started_at": None,
                "last_sweep_finished_at": None, "searches_used": 0}
    return dict(row)


def _set_state(tenant_id, **fields):
    existing = db.query_one("SELECT tenant_id FROM promotion_state WHERE tenant_id = ?",
                            (tenant_id,))
    fields["updated_at"] = clock.now_iso()
    if existing is None:
        payload = {"tenant_id": tenant_id, "last_sweep_started_at": None,
                   "last_sweep_finished_at": None, "searches_used": 0}
        payload.update(fields)
        db.insert("promotion_state", payload)
    else:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        db.execute(f"UPDATE promotion_state SET {assignments} WHERE tenant_id = ?",
                   tuple(fields.values()) + (tenant_id,))


def _spend_search(tenant_id):
    db.execute(
        "UPDATE promotion_state SET searches_used = searches_used + 1, updated_at = ? "
        "WHERE tenant_id = ?",
        (clock.now_iso(), tenant_id),
    )


def sweep_due(tenant_id=None):
    finished = state(tenant_id)["last_sweep_finished_at"]
    if not finished:
        return True
    days = clock.days_since(finished)
    return days is None or days > PROMO_SWEEP_STALE_DAYS


def pending_jobs(tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM job_run WHERE tenant_id = ? AND kind LIKE 'PROMO%' "
        "AND status IN (?, ?)",
        (tenant_id, jobs.PENDING, jobs.RUNNING),
    )
    return row["n"] if row else 0


# ---------------------------------------------------------------------------
# query planning
# ---------------------------------------------------------------------------

CATEGORY_QUERY_FAMILIES = {
    "PLAYLIST_PLATFORM": ['"{genre}" playlist submission platform',
                          '"{microgenre}" playlist curator submission service'],
    "CURATOR_NETWORK": ['"{genre}" curator network paid submission'],
    "BLOG_SUBMISSION": ['"{genre}" music blog paid submission'],
    "PAID_EDITORIAL": ['"{genre}" paid editorial consideration music'],
    "PRESS_PR": ['{territory} {genre} music PR submission',
                 '"{genre}" music publicist independent artist'],
    "RADIO": ['{genre} radio promotion independent artist',
              'college radio promotion independent artist'],
    "DJ_POOL": ['{genre} DJ pool promo service'],
    "CLUB": ['{genre} club promotion service'],
    "CREATOR": ['{genre} creator promotion campaign service'],
    "VIDEO": ['{genre} music video promotion service'],
    "SYNC": ['{genre} sync submission service'],
    "SHOWCASE": ['{genre} showcase festival submission fee'],
    "ADVERTISING": ['music advertising agency {genre}',
                    'social media advertising guide independent musicians'],
    "DSP_NATIVE": ['{genre} spotify promotion agency legitimate'],
    "DISCOVERY_NETWORK": ['{microgenre} music discovery network submission'],
    "INDIE_MARKETING": ['independent artist marketing service {genre}'],
}


def plan_queries(tenant_id=None, limit=None):
    """Query families from profile values the artist actually supplied.

    A field nobody filled in produces no query rather than a guessed one, and
    the families are interleaved so a long genre list cannot starve the rest.
    """
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    limit = limit or SWEEP_SEARCH_CAP

    genres, microgenres, territories = [], [], []
    for row in db.query(
        "SELECT f.field, f.value_json FROM track_profile_field f "
        "JOIN track_profile p ON p.id = f.profile_id WHERE p.tenant_id = ? "
        "AND f.field IN ('primary_genre','secondary_genres','microgenres','geographic_affinity') "
        "ORDER BY p.created_at, f.generated_at",
        (tenant_id,),
    ):
        try:
            value = json.loads(row["value_json"]) if row["value_json"] else None
        except (TypeError, ValueError):
            continue
        if value is None:
            continue
        if row["field"] == "primary_genre":
            genres.append(str(value))
        elif row["field"] == "secondary_genres":
            genres.extend(str(item) for item in value)
        elif row["field"] == "microgenres":
            microgenres.extend(str(item) for item in value)
        elif row["field"] == "geographic_affinity":
            territories.extend(str(item) for item in value)
    for campaign in campaigns.list_campaigns(tenant_id):
        territories.extend(json.loads(campaign["territories_json"] or "[]"))

    genres = list(dict.fromkeys(g.strip() for g in genres if g and g.strip()))
    microgenres = list(dict.fromkeys(m.strip() for m in microgenres if m and m.strip()))
    territories = list(dict.fromkeys(t.strip() for t in territories if t and t.strip()))

    families, seen = {}, set()

    def add(family, text):
        text = " ".join(str(text).split())
        if not text or "{" in text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        families.setdefault(family, []).append({"query": text, "family": family})

    for family, templates in CATEGORY_QUERY_FAMILIES.items():
        for template in templates:
            for genre in genres[:2]:
                add(family, template.format(genre=genre, microgenre=genre,
                                            territory=territories[0] if territories else ""))
            for microgenre in microgenres[:2]:
                add(family, template.format(genre=microgenre, microgenre=microgenre,
                                            territory=territories[0] if territories else ""))
    for name in SEED_SERVICES:
        add("SEED", f'"{name}" pricing submission terms')

    ordered, buckets = [], [list(items) for _family, items in sorted(families.items())]
    while buckets and len(ordered) < limit:
        for bucket in list(buckets):
            if not bucket:
                buckets.remove(bucket)
                continue
            ordered.append(bucket.pop(0))
            if len(ordered) >= limit:
                break
    return ordered


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

def start_sweep(tenant_id=None, autostarted=False):
    rbac.require("campaign.run_discovery")
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    job_id = jobs.enqueue(
        "PROMO_SWEEP", {"promo_version": PROMO_VERSION},
        idempotency_key=f"promo:sweep:{tenant_id}:{clock.now_iso()}",
        tenant_id=tenant_id,
    )
    audit.record("promotion.sweep_autostarted" if autostarted else "promotion.sweep_started",
                 entity_type="job_run", entity_id=job_id, payload={},
                 actor_kind=audit.ACTOR_SYSTEM if autostarted else audit.ACTOR_USER)
    return job_id


@jobs.register("PROMO_SWEEP")
def _promo_sweep(context):
    if context.cancelled():
        return None
    tenant_id = context.tenant_id
    _set_state(tenant_id, last_sweep_started_at=clock.now_iso(), searches_used=0)

    queries = plan_queries(tenant_id)
    for planned in queries:
        context.enqueue("PROMO_SEARCH", {
            "query": planned["query"], "family": planned["family"], "sweep_id": context.id,
        }, idempotency_key=f"promo:search:{context.id}:{planned['query']}")

    policies = 0
    for slug, url in PLATFORM_POLICY_URLS.items():
        if _policy_evidence_fresh(tenant_id, slug):
            continue
        context.enqueue("PROMO_FETCH", {"url": url, "policy_slug": slug,
                                        "sweep_id": context.id},
                        idempotency_key=f"promo:policy:{context.id}:{slug}")
        policies += 1

    for service in _rescreen_due(tenant_id):
        for url in refetch_set(service):
            context.enqueue("PROMO_FETCH", {"url": url, "service_id": service["id"],
                                            "sweep_id": context.id},
                            idempotency_key=f"promo:fetch:{context.id}:{url}")
        context.enqueue("PROMO_SCREEN", {"service_id": service["id"],
                                         "sweep_id": context.id},
                        idempotency_key=f"promo:screen:{context.id}:{service['id']}")

    context.progress(0, len(queries), "Researching paid promotion services")
    audit.record("promotion.sweep_planned", entity_type="job_run", entity_id=context.id,
                 payload={"queries": len(queries), "policy_fetches": policies})
    return {"queries": len(queries), "policy_fetches": policies,
            "search_cap": SWEEP_SEARCH_CAP}


def _policy_evidence_fresh(tenant_id, slug):
    row = db.query_one(
        "SELECT * FROM evidence_packet WHERE tenant_id = ? AND entity_type = ? "
        "AND entity_id = ? ORDER BY retrieved_at DESC LIMIT 1",
        (tenant_id, "promotion_policy", slug),
    )
    return row is not None and not evidence.is_stale(row)


def _rescreen_due(tenant_id):
    now = clock.now_iso()
    return db.query(
        "SELECT * FROM promotion_service WHERE tenant_id = ? AND canonical_id IS NULL "
        "AND (next_review_at IS NULL OR next_review_at <= ?)",
        (tenant_id, now),
    )


def refetch_set(service):
    """Every URL a rescreen has to re-read.

    The canonical landing page is always in the set — a homepage that quietly
    adds "guaranteed streams" has to be caught — along with every URL currently
    backing a signal or a SCREENED-condition fact.
    """
    urls = []
    for url in (service["url"], service["terms_url"], service["pricing_url"],
                service["submission_url"], service["contact_url"]):
        if url and url not in urls:
            urls.append(url)
    for row in current_packets(service["id"]):
        if row["source_url"] and row["source_url"] not in urls:
            urls.append(row["source_url"])
    return urls


@jobs.register("PROMO_SEARCH")
def _promo_search(context):
    if context.cancelled():
        return None
    tenant_id = context.tenant_id
    used = state(tenant_id)["searches_used"]
    if used >= SWEEP_SEARCH_CAP:
        return {"skipped": "SEARCH_CAP", "searches_used": used, "cap": SWEEP_SEARCH_CAP}

    query = context.payload["query"]
    response = search_provider.search(query, limit=RESULTS_PER_SEARCH)
    _spend_search(tenant_id)
    context.cost(searches=1)

    enqueued, platform_skipped = 0, 0
    for result in response.items:
        url = result.get("url")
        if not url:
            continue
        try:
            validated = netguard.validate_url(url, resolve=False)
        except FetchBlocked:
            continue
        if entities.is_platform_domain(validated["domain"]):
            # A DSP, a social network or a marketplace is never itself a
            # promotion service REACH can screen as a company.
            platform_skipped += 1
            continue
        context.enqueue("PROMO_FETCH", {"url": url, "sweep_id": context.payload.get("sweep_id")},
                        idempotency_key=f"promo:fetch:{context.payload.get('sweep_id')}:{url}")
        enqueued += 1

    return {"query": query, "family": context.payload.get("family"),
            "results": len(response.items), "fetch_jobs": enqueued,
            "platform_skipped": platform_skipped, "mode": response.mode,
            "note": response.note}


def _hostname(url):
    return urlsplit(url).hostname or ""


@jobs.register("PROMO_FETCH")
def _promo_fetch(context):
    if context.cancelled():
        return None
    tenant_id = context.tenant_id
    url = context.payload["url"]
    policy_slug = context.payload.get("policy_slug")

    try:
        result = fetcher.fetch(url)
    except FetchBlocked as exc:
        # A refusal is a result: the attempt is on record with its reason, and
        # whatever the page would have said stays UNKNOWN.
        evidence.record_source_document(
            url=url, domain=netguard.registrable_domain(_hostname(url)),
            provider="open_web_fetch", fetch_status=evidence.FETCH_BLOCKED,
            robots_decision="N/A", block_reason=exc.reason, tenant_id=tenant_id,
        )
        return {"blocked": exc.reason, "url": url}

    context.cost(pages=1)
    sanitized = sanitizer.sanitize(result.text(), base_url=result.final_url)
    page_state = extractor.page_state(sanitized, http_status=result.status)
    content_hash = crypto.content_hash(sanitized["visible_text"])

    # record_source_document upserts and overwrites content_hash in place, so
    # the prior hash has to be read BEFORE recording or the comparison is lost.
    prior = db.query_one(
        "SELECT content_hash FROM source_document WHERE tenant_id = ? AND url_hash = ? "
        "AND campaign_id IS NULL",
        (tenant_id, crypto.url_hash(result.final_url)),
    )
    prior_hash = prior["content_hash"] if prior else None

    document_id = evidence.record_source_document(
        url=result.final_url, domain=result.domain, provider="open_web_fetch",
        fetch_status=evidence.FETCH_OK, http_status=result.status, mime=result.mime,
        byte_count=result.bytes, sanitized_text=sanitized["visible_text"],
        title=sanitized["title"], robots_decision=result.robots_decision,
        tenant_id=tenant_id,
    )

    if page_state != extractor.PAGE_OK:
        # A bot challenge or an error page is not the site behind it: nothing
        # on it may become evidence about a business.
        audit.record("promotion.page_unusable", entity_type="source_document",
                     entity_id=document_id,
                     payload={"url": result.final_url, "page_state": page_state})
        return {"url": result.final_url, "page_state": page_state, "skipped": True,
                "document_id": document_id}

    if policy_slug:
        # The policy branch: DSP policy pages live on exactly the platform
        # domains the service gate exists to drop, so both are skipped here.
        # No service entity is ever created from one.
        evidence.record(
            entity_type="promotion_policy", entity_id=policy_slug,
            supports_field="platform_policy",
            value={"slug": policy_slug, "title": sanitized["title"]},
            source_url=result.final_url, source_domain=result.domain,
            source_type="OFFICIAL", excerpt=sanitized["visible_text"][:400],
            confidence=0.9, extractor_version=PROMO_VERSION, content_hash=content_hash,
            source_document_id=document_id, tenant_id=tenant_id,
        )
        return {"policy_slug": policy_slug, "url": result.final_url}

    if entities.is_platform_domain(result.domain):
        return {"skipped": "PLATFORM_DOMAIN", "domain": result.domain}

    service_id = context.payload.get("service_id")
    if service_id is None:
        # The gate is on *creating* a service, not on reading one. Once a
        # domain has made an offer somewhere, its terms and pricing pages are
        # part of that service's evidence even though they sell nothing
        # themselves.
        known = service_by_domain(result.domain, tenant_id)
        if known is not None:
            service_id = known["id"]
        elif not is_service_offer_page(sanitized):
            # A page that writes ABOUT promotion services is a source document
            # and nothing more. There is no honest way to turn one domain's
            # opinion into another domain's business record.
            return {"skipped": "NOT_A_SERVICE_OFFER", "url": result.final_url,
                    "document_id": document_id}
        else:
            name = extractor.outlet_name(sanitized, result.domain)
            service_id = ensure_service(name, result.domain, result.final_url, tenant_id)

    changed = bool(prior_hash and prior_hash != content_hash)
    recorded = _record_page_evidence(service_id, sanitized, result, document_id,
                                     content_hash, tenant_id)

    if changed and recorded["important"]:
        db.update("promotion_service", service_id, {"manual_review_required": 1,
                                                    "updated_at": clock.now_iso()})
        audit.record("promotion.policy_changed", entity_type="promotion_service",
                     entity_id=service_id, payload={"url": result.final_url})

    # Follow the service's own terms, pricing and submission links. Refunds and
    # placement policy live on those pages, so a sweep that only ever reads the
    # page a search returned can never establish enough to screen anything.
    followed = _follow_declared_links(context, service_id, recorded["links"], tenant_id)

    context.enqueue("PROMO_SCREEN", {"service_id": service_id,
                                     "sweep_id": context.payload.get("sweep_id")},
                    idempotency_key=(f"promo:screen:{context.payload.get('sweep_id')}:"
                                     f"{service_id}:{clock.now_iso()}"))
    return {"service_id": service_id, "url": result.final_url,
            "signals": len(recorded["signals"]), "facts": len(recorded["facts"]),
            "followed": followed,
            "policy_changed": changed and recorded["important"]}


# How many of a service's own declared pages one fetch may pull in. Bounded so
# a link-heavy footer cannot turn one result into a crawl.
MAX_FOLLOWED_LINKS = 4


def _follow_declared_links(context, service_id, links, tenant_id):
    """Enqueue the service's own terms/pricing/submission pages, once each.

    Same guards as any other fetch — these go through the identical PROMO_FETCH
    handler, with service_id set so the offer gate does not apply to a page
    that sells nothing.
    """
    seen = {row["source_url"] for row in evidence.for_entity("promotion_service", service_id)}
    followed = []
    for field in ("terms_url", "pricing_url", "submission_url", "contact_url"):
        url = links.get(field)
        if not url or url in seen or len(followed) >= MAX_FOLLOWED_LINKS:
            continue
        try:
            validated = netguard.validate_url(url, resolve=False)
        except FetchBlocked:
            continue
        if entities.is_platform_domain(validated["domain"]):
            continue
        seen.add(url)
        followed.append(url)
        context.enqueue("PROMO_FETCH", {"url": url, "service_id": service_id,
                                        "sweep_id": context.payload.get("sweep_id")},
                        idempotency_key=(f"promo:follow:{context.payload.get('sweep_id')}:"
                                         f"{service_id}:{url}"))
    return followed


# Categories whose change on a page is worth a human's attention.
_IMPORTANT_SUPPORTS = {"stream_guarantees", "pricing", "curator_compensation",
                       "anti_bot_policy", "placement_policy", "refund_policy", "terms"}


def _record_page_evidence(service_id, sanitized, result, document_id, content_hash,
                          tenant_id):
    """Every extracted signal and fact, each with the passage that justified it."""
    service = get_service(service_id)
    url, domain = result.final_url, result.domain

    def packet(supports_field, value, excerpt, confidence):
        return evidence.record(
            entity_type="promotion_service", entity_id=service_id,
            supports_field=supports_field, value=value, source_url=url,
            source_domain=domain, source_type="OFFICIAL", excerpt=excerpt,
            confidence=confidence, extractor_version=PROMO_VERSION,
            content_hash=content_hash, source_document_id=document_id, tenant_id=tenant_id,
        )

    signals = hard_block_signals(sanitized)
    for signal in corroborating_classification(sanitized):
        if signal["signal"] not in {item["signal"] for item in signals}:
            signals.append(signal)
    for signal in signals:
        packet("stream_guarantees", {"signal": signal["signal"], "label": signal["label"]},
               signal["excerpt"], 0.9)

    facts = extract_facts(sanitized)
    for fact in facts:
        packet(FIELD_SUPPORTS.get(fact["field"], "business_model"),
               {"field": fact["field"], "value": fact["value"], "label": fact["label"]},
               fact["excerpt"], fact["confidence"])

    payload = {"updated_at": clock.now_iso()}
    category, category_excerpt = extract_category(sanitized, with_excerpt=True)
    service_types, types_excerpt = extract_service_types(sanitized, with_excerpt=True)
    genres = extractor.genres(sanitized)
    if category != CATEGORY_UNKNOWN or service_types:
        # The packet carries the passage that justified the claim; the columns
        # themselves are recomputed from current packets afterwards, so a page
        # that stops saying something stops asserting it.
        packet("business_model",
               {"category": category, "service_types": service_types, "genres": genres},
               category_excerpt or types_excerpt
               or (sanitized.get("meta_description") or sanitized["visible_text"])[:300], 0.6)

    pricing = extract_pricing(sanitized)
    if pricing is None:
        login_excerpt = pricing_login_wall(sanitized, url=url,
                                           pricing_url=service["pricing_url"])
        if login_excerpt:
            # A login wall is a real answer about why the price is unknown, and
            # it is where REACH stops: no account, no reading behind it.
            packet("pricing", {"login_walled": True}, login_excerpt, 0.7)
    if pricing is not None:
        packet("pricing", {"min": pricing["min"], "max": pricing["max"],
                           "currency": pricing["currency"], "model": pricing["model"]},
               pricing["excerpt"], 0.7)

    links = _same_domain_links(sanitized, domain)
    for field, href in links.items():
        if not service[field]:
            payload[field] = href
    if links.get("terms_url") or links.get("contact_url"):
        label = "terms" if links.get("terms_url") else "contact"
        match = re.search(label, _joined(sanitized), re.I)
        packet("terms", {"terms_url": links.get("terms_url"),
                         "contact_url": links.get("contact_url")},
               _excerpt_around(_joined(sanitized), match) if match
               else sanitized["visible_text"][:200], 0.6)
    if sanitized.get("title"):
        payload.setdefault("company_name", service["company_name"] or sanitized["title"][:120])
        packet("company_identity", {"name": sanitized["title"][:120], "domain": domain},
               sanitized["title"][:200], 0.6)

    db.update("promotion_service", service_id, payload)

    important = bool(signals) or any(
        FIELD_SUPPORTS.get(fact["field"], "business_model") in _IMPORTANT_SUPPORTS
        for fact in facts) or pricing is not None or bool(links.get("terms_url"))
    return {"signals": signals, "facts": facts, "links": links, "important": important}


@jobs.register("PROMO_SCREEN")
def _promo_screen(context):
    if context.cancelled():
        return None
    service_id = context.payload["service_id"]
    result = run_screening(service_id)
    if result is None:
        return None
    return {"service_id": service_id, "status": result["effective_status"],
            "automated_status": result["status"], "score": result["score"]}


def request_rescan(service_id):
    """A human asking for an immediate refresh.

    Idempotency keys are request-scoped: a permanent key would make every
    re-request a silent no-op.
    """
    rbac.require("promotion.screen")
    service = get_service(service_id)
    if service is None:
        raise ValidationError("Unknown promotion service")
    stamp = clock.now_iso()
    for url in refetch_set(service):
        jobs.enqueue("PROMO_FETCH", {"url": url, "service_id": service_id},
                     idempotency_key=f"promo:refetch:{service_id}:{url}:{stamp}",
                     tenant_id=service["tenant_id"])
    job_id = jobs.enqueue("PROMO_SCREEN", {"service_id": service_id},
                          idempotency_key=f"promo:rescan:{service_id}:{stamp}",
                          tenant_id=service["tenant_id"])
    audit.record("promotion.rescan_requested", entity_type="promotion_service",
                 entity_id=service_id, payload={}, actor_kind=audit.ACTOR_USER)
    return job_id


def run_to_completion(max_jobs=500, max_seconds=None, tenant_id=None):
    """Drain promotion jobs in a bounded chunk, like discovery and radar."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    jobs.requeue_stale()
    started = time.monotonic()
    processed = 0
    while processed < max_jobs and pending_jobs(tenant_id):
        if max_seconds is not None and time.monotonic() - started >= max_seconds:
            break
        job = jobs.run_one()
        if job is None:
            break
        processed += 1
    _maybe_finish(tenant_id)
    return processed


def skipped_queries(tenant_id=None):
    """Planned queries the search cap stopped REACH from running."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM job_run WHERE tenant_id = ? AND kind = 'PROMO_SEARCH' "
        "AND result_json LIKE '%SEARCH_CAP%'",
        (tenant_id,),
    )
    return row["n"] if row else 0


def _maybe_finish(tenant_id):
    if pending_jobs(tenant_id):
        return
    current = state(tenant_id)
    started, finished = current["last_sweep_started_at"], current["last_sweep_finished_at"]
    if started and (not finished or finished < started):
        _set_state(tenant_id, last_sweep_finished_at=clock.now_iso())
        # A sweep that ran out of budget is not a sweep that finished the work.
        audit.record("promotion.sweep_finished", entity_type="promotion_state",
                     entity_id=tenant_id,
                     payload={"searches_used": state(tenant_id)["searches_used"],
                              "search_cap": SWEEP_SEARCH_CAP,
                              "queries_not_run": skipped_queries(tenant_id)})


# ---------------------------------------------------------------------------
# dismissals
# ---------------------------------------------------------------------------

def dismiss(service_id, campaign_id):
    """"Not for this release" — never a decline, never a plan row."""
    rbac.require("promotion.plan")
    service = get_service(service_id)
    if service is None:
        raise ValidationError("Unknown promotion service")
    existing = db.query_one(
        "SELECT id FROM promotion_dismissal WHERE campaign_id = ? AND service_id = ?",
        (campaign_id, service_id),
    )
    if existing is not None:
        return existing["id"]
    dismissal_id = db.new_id("dism")
    db.insert("promotion_dismissal", {
        "id": dismissal_id,
        "tenant_id": service["tenant_id"],
        "campaign_id": campaign_id,
        "service_id": service_id,
        "created_at": clock.now_iso(),
    })
    audit.record("promotion.dismissed", entity_type="promotion_service", entity_id=service_id,
                 payload={"campaign_id": campaign_id}, actor_kind=audit.ACTOR_USER)
    return dismissal_id


def dismissed_ids(campaign_id):
    return {row["service_id"] for row in db.query(
        "SELECT service_id FROM promotion_dismissal WHERE campaign_id = ?", (campaign_id,))}


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def add_plan_item(campaign_id, service_id=None, label=None, category="OTHER", amount=None,
                  currency=None, note=None):
    rbac.require("promotion.plan")
    campaign = campaigns.get(campaign_id)
    if campaign is None:
        raise ValidationError("Unknown campaign")
    # The two vocabularies meet here rather than in every caller: a service
    # type is translated, an allocation category passes through, anything else
    # is refused.
    category = allocation_category_for(category)

    service = None
    if service_id:
        service = canonical_service(service_id)
        if service is None:
            raise ValidationError("Unknown promotion service")
        if service["screening_status"] == BLOCKED:
            raise ValidationError(
                f"{service['name']} is BLOCKED: {service['block_reason'] or BLOCKED_STATEMENT} "
                "REACH will not add a blocked service to a promotion plan."
            )
        service_id = service["id"]
    label = (label or (service["name"] if service else "")).strip()
    if not label:
        raise ValidationError("A plan item needs a label")

    parsed = None
    if amount is not None and str(amount).strip() != "":
        try:
            parsed = float(amount)
        except (TypeError, ValueError):
            raise ValidationError("Amount must be a number, or left blank for UNKNOWN")

    now = clock.now_iso()
    item_id = db.new_id("plan")
    db.insert("promotion_plan_item", {
        "id": item_id,
        "tenant_id": campaign["tenant_id"],
        "campaign_id": campaign_id,
        "service_id": service_id,
        "label": label[:200],
        "allocation_category": category,
        "amount": parsed,
        "currency": currency or campaign["promotion_budget_currency"],
        "status": PLANNED,
        "paid_at": None,
        "note": note,
        "created_at": now,
        "updated_at": now,
    })
    audit.record("promotion.plan_added", entity_type="promotion_plan_item", entity_id=item_id,
                 payload={"campaign_id": campaign_id, "service_id": service_id,
                          "category": category}, actor_kind=audit.ACTOR_USER)
    return item_id


def set_plan_status(item_id, status):
    rbac.require("promotion.plan")
    if status not in PLAN_STATUSES:
        raise ValidationError(f"Unknown plan status: {status}")
    item = db.query_one("SELECT * FROM promotion_plan_item WHERE id = ?", (item_id,))
    if item is None:
        raise ValidationError("Unknown plan item")
    payload = {"status": status, "updated_at": clock.now_iso()}
    if status == PAID and not item["paid_at"]:
        payload["paid_at"] = clock.now_iso()
    if status == REFUNDED:
        payload["paid_at"] = None
    db.update("promotion_plan_item", item_id, payload)
    audit.record("promotion.plan_status", entity_type="promotion_plan_item", entity_id=item_id,
                 payload={"status": status}, actor_kind=audit.ACTOR_USER)
    return status


def remove_plan_item(item_id):
    rbac.require("promotion.plan")
    db.execute("DELETE FROM promotion_plan_item WHERE id = ?", (item_id,))
    audit.record("promotion.plan_removed", entity_type="promotion_plan_item",
                 entity_id=item_id, payload={}, actor_kind=audit.ACTOR_USER)
    return True


def plan_items(campaign_id):
    return db.query(
        "SELECT p.*, s.name AS service_name, s.screening_status "
        "FROM promotion_plan_item p LEFT JOIN promotion_service s ON s.id = p.service_id "
        "WHERE p.campaign_id = ? ORDER BY p.created_at",
        (campaign_id,),
    )


def plan_totals(campaign_id):
    """Payment and intent, counted separately.

    A declined pitch usually keeps the fee, so a paid item stays Spent whatever
    the curator decided. An amount nobody entered is UNKNOWN and joins no sum.
    """
    campaign = campaigns.get(campaign_id)
    items = plan_items(campaign_id)
    spent = 0.0
    planned = 0.0
    unknown_amounts = 0
    for item in items:
        if item["amount"] is None:
            unknown_amounts += 1
            continue
        if item["paid_at"] and item["status"] != REFUNDED:
            spent += item["amount"]
        elif not item["paid_at"] and item["status"] in PLANNED_STATUSES:
            planned += item["amount"]
    budget = campaign["promotion_budget_amount"] if campaign else None
    remaining = None if budget is None else budget - (planned + spent)
    return {
        "budget": budget,
        "currency": campaign["promotion_budget_currency"] if campaign else None,
        "planned": planned,
        "spent": spent,
        "remaining": remaining,
        "unknown_amounts": unknown_amounts,
        "items": len(items),
    }


def suggested_allocation(campaign_id):
    """An even split of what is left across categories that actually have a
    screened option — never proportional to how many results a query family
    happened to return, and never a projection of results."""
    totals = plan_totals(campaign_id)
    remaining = totals["remaining"]
    if remaining is None or remaining <= 0:
        return None
    # Items with no amount are real commitments REACH could not price. What is
    # "left" is therefore an upper bound, and the panel has to say so.
    caveat = None
    if totals["unknown_amounts"]:
        caveat = (f"{totals['unknown_amounts']} planned item"
                  f"{'s' if totals['unknown_amounts'] != 1 else ''} "
                  "have no amount and are not subtracted.")
    campaign = campaigns.get(campaign_id)
    dismissed = dismissed_ids(campaign_id)
    qualifying = set()
    for service in services(campaign["tenant_id"]):
        if service["screening_status"] != SCREENED or service["id"] in dismissed:
            continue
        if latest_fit(service["id"], campaign_id) is None:
            continue
        for service_type in json.loads(service["service_types_json"] or "[]"):
            qualifying.add(SERVICE_TYPE_TO_ALLOCATION.get(service_type, "OTHER"))
        if not json.loads(service["service_types_json"] or "[]"):
            qualifying.add("OTHER")
    if not qualifying:
        return None
    categories = sorted(qualifying)
    share = round(remaining / len(categories), 2)
    return {
        "categories": [{"category": category, "amount": share} for category in categories],
        "basis": ALLOCATION_BASIS.format(n=len(categories)),
        "caveat": caveat,
        "currency": totals["currency"],
        "remaining": remaining,
    }


# ---------------------------------------------------------------------------
# Needs You handoff
# ---------------------------------------------------------------------------

def create_handoff_task(service_id, campaign_id):
    """A copy-ready submission task — only ever for a SCREENED service."""
    from . import catalog, humanactions

    service = canonical_service(service_id)
    if service is None:
        raise ValidationError("Unknown promotion service")
    if service["screening_status"] != SCREENED:
        raise ValidationError(
            "REACH only prepares a submission handoff for a screened service. "
            f"{service['name']} is {service['screening_status']}."
        )
    campaign = campaigns.get(campaign_id)
    if campaign is None:
        raise ValidationError("Unknown campaign")

    recording = catalog.get_recording(campaign["recording_id"])
    facts = catalog.track(recording)
    profile_id = profile.get_or_create(campaign["recording_id"])
    values = profile.values(profile_id)
    fields = humanactions._copy_ready_answers(recording, facts, values)
    fields.append({"label": "Campaign notes", "value": campaign["name"] or "UNKNOWN"})
    fields.append({"label": "Private streaming link",
                   "value": "UNKNOWN — add before submitting"})
    fields.append({"label": "Artwork", "value": "UNKNOWN — attach before submitting"})
    if service["pricing_min"] is None:
        # needs_you renders cost only when it is truthy, so a 0.0 stand-in for
        # an unread price would present a paid submission as free.
        fields.append({"label": "Cost",
                       "value": "UNKNOWN — REACH could not read this service's price"})

    slug = re.sub(r"[^a-z0-9]+", "_", (service["name"] or service["canonical_domain"]).lower())
    # No target_id: a promotion purchase is not outreach, and must never flip a
    # campaign into the Outreach stage or record a submission against a target.
    return humanactions.create(
        campaign_id, provider=slug.strip("_") or "promotion",
        title=f"Submit to {service['name']}",
        action="Complete this service's own submission and payment process",
        reason=humanactions.REASON_PAID,
        destination_url=service["submission_url"] or service["url"],
        fields=fields,
        eligibility=[
            {"label": "Service screened by REACH", "ok": True},
            {"label": "Placement stays discretionary",
             "ok": True if service["placement_discretionary"] == TRUE else None},
            {"label": "Spend approved by you", "ok": None},
        ],
        cost=service["pricing_min"] if service["pricing_min"] is not None else 0.0,
        currency=service["pricing_currency"],
        effort=humanactions.EFFORT_MEDIUM,
    )


# ---------------------------------------------------------------------------
# campaign settings + listings for the web layer
# ---------------------------------------------------------------------------

def set_campaign_promotion(campaign_id, enabled, budget_amount=None, budget_currency=None,
                           allocations=None):
    rbac.require("campaign.create")
    campaign = campaigns.get(campaign_id)
    if campaign is None:
        raise ValidationError("Unknown campaign")
    parsed = None
    if budget_amount is not None and str(budget_amount).strip() != "":
        try:
            parsed = float(budget_amount)
        except (TypeError, ValueError):
            raise ValidationError("Budget must be a number, or left blank for UNKNOWN")
    payload = {
        "paid_promotion_enabled": 1 if enabled else 0,
        "promotion_budget_amount": parsed,
        "promotion_budget_currency": (budget_currency or None),
        "updated_at": clock.now_iso(),
    }
    if allocations is not None:
        payload["promotion_allocations_json"] = json.dumps(allocations)
    db.update("campaign", campaign_id, payload)
    audit.record("promotion.enabled" if enabled else "promotion.disabled",
                 entity_type="campaign", entity_id=campaign_id,
                 payload={"budget": parsed, "currency": budget_currency},
                 actor_kind=audit.ACTOR_USER)
    return True


def enabled_for(campaign_id):
    row = db.query_one("SELECT paid_promotion_enabled FROM campaign WHERE id = ?",
                       (campaign_id,))
    return bool(row and row["paid_promotion_enabled"])


# Safety leads: services are grouped by screening status, and campaign fit only
# ranks within a group.
_STATUS_ORDER = {SCREENED: 0, CAUTION: 1, UNKNOWN: 2, BLOCKED: 3}

VIEWS = ["all", "best-fit", "screened", "caution", "unknown", "blocked"]


def campaign_services(campaign_id, view="all", filters=None):
    """The service list a campaign screen renders, safety-grouped."""
    campaign = campaigns.get(campaign_id)
    if campaign is None:
        return []
    filters = filters or {}
    dismissed = dismissed_ids(campaign_id)
    planned = {row["service_id"] for row in plan_items(campaign_id) if row["service_id"]}

    items = []
    for service in services(campaign["tenant_id"]):
        if service["id"] in dismissed:
            continue
        status = service["screening_status"]
        if view == "screened" and status != SCREENED:
            continue
        if view == "caution" and status != CAUTION:
            continue
        if view == "unknown" and status != UNKNOWN:
            continue
        if view == "blocked" and status != BLOCKED:
            continue
        if view == "best-fit" and status != SCREENED:
            continue

        service_types = json.loads(service["service_types_json"] or "[]")
        genres = json.loads(service["supported_genres_json"] or "[]")
        territories = json.loads(service["supported_territories_json"] or "[]")
        if filters.get("type") and filters["type"] not in service_types:
            continue
        if filters.get("genre") and filters["genre"].lower() not in [g.lower() for g in genres]:
            continue
        if filters.get("territory") and filters["territory"].upper() not in [
                t.upper() for t in territories]:
            continue
        if filters.get("channel") and filters["channel"] not in [
                _channel_for(t) for t in service_types]:
            continue
        if filters.get("price_max") is not None:
            if service["pricing_min"] is None or service["pricing_min"] > filters["price_max"]:
                continue

        fit = latest_fit(service["id"], campaign_id)
        items.append({
            "service": service,
            "status": status,
            "override": effective_override(service),
            "stale": is_stale(service),
            "needs_rescreen": bool(service["manual_review_required"]),
            "service_types": service_types,
            "fit_score": fit["score"] if fit and status != BLOCKED else None,
            "fit_components": (json.loads(fit["components_json"] or "{}")
                               if fit and status != BLOCKED else None),
            "reasons": _latest_reasons(service["id"]),
            "pricing_current": pricing_is_current(service),
            "in_plan": service["id"] in planned,
        })

    if view == "best-fit":
        items.sort(key=lambda item: (item["fit_score"] is None,
                                     -(item["fit_score"] or 0), item["service"]["name"]))
    else:
        items.sort(key=lambda item: (_STATUS_ORDER.get(item["status"], 9),
                                     item["fit_score"] is None,
                                     -(item["fit_score"] or 0), item["service"]["name"]))
    return items


def _latest_reasons(service_id):
    row = latest_screening(service_id)
    if row is None:
        return []
    try:
        return json.loads(row["reasons_json"] or "[]")
    except (TypeError, ValueError):
        return []


def pricing_is_current(service):
    """Is the price fresh enough to show as current?

    A stale price is shown with the date it was read, never as today's price.
    """
    if service["pricing_min"] is None or not service["pricing_last_verified_at"]:
        return False
    days = clock.days_since(service["pricing_last_verified_at"])
    return days is not None and days <= rescreen_window(service["screening_status"])


def evidence_view(service_id):
    """Every packet, marked current or superseded and stale or fresh.

    Evidence is append-only, so the panel would otherwise present a claim a
    later read of the same page already replaced as though it still stood.
    """
    current_ids = {row["id"] for row in current_packets(service_id)}
    items = []
    for item in evidence.summary("promotion_service", service_id):
        items.append({**item, "superseded": item["id"] not in current_ids})
    return items


def review_queue(tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    return db.query(
        "SELECT * FROM promotion_service WHERE tenant_id = ? AND canonical_id IS NULL "
        "AND manual_review_required = 1 ORDER BY updated_at DESC",
        (tenant_id,),
    )


def merge_suggestions(tenant_id=None):
    """Same normalized name on different domains — a suggestion, never applied."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    by_name = {}
    for service in services(tenant_id):
        key = "".join(ch for ch in (service["name"] or "").lower() if ch.isalnum())
        if key:
            by_name.setdefault(key, []).append(service)
    return [{"reason": "Identical normalized service name",
             "candidates": [dict(item) for item in group]}
            for group in by_name.values() if len(group) > 1]


def dashboard_counts(tenant_id=None):
    """Counts the dashboard's next-best-actions render, or zeros."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    enabled = [row for row in campaigns.list_campaigns(tenant_id)
               if row["paid_promotion_enabled"]]
    if not enabled:
        return {"screened": 0, "matched": 0, "review": 0, "campaign_id": None}

    campaign_id = enabled[0]["id"]
    dismissed = dismissed_ids(campaign_id)
    planned = {row["service_id"] for row in plan_items(campaign_id) if row["service_id"]}
    screened, matched = 0, 0
    for service in services(tenant_id):
        if service["screening_status"] != SCREENED or service["id"] in dismissed:
            continue
        screened += 1
        if service["id"] not in planned and latest_fit(service["id"], campaign_id) is not None:
            matched += 1
    return {"screened": screened, "matched": matched,
            "review": len(review_queue(tenant_id)), "campaign_id": campaign_id}
