"""Schema-constrained extraction from sanitized page text.

Extraction is deterministic: keyword and pattern rules over sanitized text,
producing only fields the schema declares. No language model is involved, so
there is no path by which page text could act as an instruction — and every
extracted value ships with the excerpt that justified it.

Where a value cannot be established, the field is UNKNOWN. UNKNOWN is never
upgraded to a default.
"""

import re

from . import sanitizer

EXTRACTOR_VERSION = "extractor/1.0.0"

# --- page classes ----------------------------------------------------------

PLAYLIST = "PLAYLIST"
CURATOR = "CURATOR"
RADIO = "RADIO"
BLOG = "BLOG"
PUBLICATION = "PUBLICATION"
DJ = "DJ"
DJ_POOL = "DJ_POOL"
CREATOR = "CREATOR"
PODCAST = "PODCAST"
SUBMISSION_GUIDELINES = "SUBMISSION_GUIDELINES"
BUSINESS_CONTACT = "BUSINESS_CONTACT"
PAID_CONSIDERATION = "PAID_CONSIDERATION"
GUARANTEED_PLACEMENT = "GUARANTEED_PLACEMENT"
GUARANTEED_STREAMS = "GUARANTEED_STREAMS"
UNKNOWN = "UNKNOWN"

CLASSIFICATIONS = [
    PLAYLIST, CURATOR, RADIO, BLOG, PUBLICATION, DJ, DJ_POOL, CREATOR, PODCAST,
    SUBMISSION_GUIDELINES, BUSINESS_CONTACT, PAID_CONSIDERATION,
    GUARANTEED_PLACEMENT, GUARANTEED_STREAMS, UNKNOWN,
]

# Outlet kinds a campaign target can be built from. The rest are page classes
# that describe a page, not an outlet.
OUTLET_KINDS = [PLAYLIST, CURATOR, RADIO, BLOG, PUBLICATION, DJ, DJ_POOL, CREATOR, PODCAST]

_CLASS_RULES = [
    # (classification, weight, pattern)
    (GUARANTEED_STREAMS, 100, re.compile(r"guarantee(?:d|s|ing)?\s+(?:you\s+)?[\d,]*\s*(?:\+\s*)?(?:streams|plays|listens)", re.I)),
    (GUARANTEED_STREAMS, 100, re.compile(r"\b(?:buy|purchase|get)\s+[\d,]+\s*(?:\+\s*)?(?:spotify\s+)?(?:streams|plays)", re.I)),
    (GUARANTEED_PLACEMENT, 100, re.compile(r"guarantee(?:d|s|ing)?\s+(?:playlist\s+)?placement", re.I)),
    (GUARANTEED_PLACEMENT, 90, re.compile(r"placement\s+guaranteed", re.I)),
    (PAID_CONSIDERATION, 40, re.compile(r"(?<!no )(?<!not )(?<!free of )\b(?:submission|review)\s+fee\s+of\b", re.I)),
    (PAID_CONSIDERATION, 40, re.compile(r"\bpay\s+(?:\$|€|£)\s*\d", re.I)),
    (PAID_CONSIDERATION, 30, re.compile(r"\bpaid\s+(?:review|consideration|promotion)\b", re.I)),
    (DJ_POOL, 60, re.compile(r"\bdj\s+pool\b|\brecord\s+pool\b|\bpromo\s+pool\b", re.I)),
    (RADIO, 55, re.compile(r"\b(?:radio\s+station|college\s+radio|community\s+radio|fm\s+\d|on\s+air|airplay|programming\s+director|music\s+director)\b", re.I)),
    (RADIO, 32, re.compile(r"\bradio\b", re.I)),
    (PODCAST, 50, re.compile(r"\bpodcast\b|\bepisode\s+\d+\b", re.I)),
    (PLAYLIST, 50, re.compile(r"\bplaylist\b", re.I)),
    (DJ, 45, re.compile(r"\bdj\s+(?:promo|mailing\s+list|charts)\b|\bmix\s?show\b", re.I)),
    (BLOG, 40, re.compile(r"\b(?:music\s+blog|blog|premiere|review\s+policy)\b", re.I)),
    (PUBLICATION, 40, re.compile(r"\b(?:magazine|editorial\s+team|editor[- ]in[- ]chief|masthead|contributors?)\b", re.I)),
    (CREATOR, 35, re.compile(r"\b(?:content\s+creator|influencer|creator\s+partnerships|business\s+enquiries)\b", re.I)),
    (CURATOR, 35, re.compile(r"\bcurator\b|\bwe\s+curate\b", re.I)),
    (SUBMISSION_GUIDELINES, 30, re.compile(r"\b(?:submission\s+guidelines|submit\s+(?:your\s+)?music|send\s+us\s+your\s+(?:music|track)|demo\s+submissions?)\b", re.I)),
    (BUSINESS_CONTACT, 20, re.compile(r"\b(?:contact\s+us|business\s+contact|press\s+contact|get\s+in\s+touch)\b", re.I)),
]

# --- submission state ------------------------------------------------------

OPEN = "OPEN"
CLOSED = "CLOSED"
SUBMISSIONS_UNKNOWN = "UNKNOWN"

_OPEN_PATTERNS = [
    re.compile(r"\b(?:submit|send)\s+(?:us\s+)?(?:your\s+)?(?:music|track|song|demo|release)\b", re.I),
    re.compile(r"\bsubmissions?\s+(?:are\s+)?open\b", re.I),
    re.compile(r"\b(?:accepts?|accepting|welcomes?)\s+(?:music\s+|new\s+)?submissions?\b", re.I),
    re.compile(r"\bdemo\s+submissions?\b", re.I),
    re.compile(r"\bsend\s+(?:us\s+)?(?:your\s+)?promos?\b", re.I),
]
_CLOSED_PATTERNS = [
    re.compile(r"\bsubmissions?\s+(?:are\s+)?(?:currently\s+)?closed\b", re.I),
    re.compile(r"\bnot\s+(?:currently\s+)?accepting\s+(?:new\s+)?(?:music|submissions?|demos?)\b", re.I),
    re.compile(r"\bno\s+unsolicited\s+(?:music|demos?|submissions?)\b", re.I),
    re.compile(r"\bplease\s+do\s+not\s+send\b", re.I),
]

# --- cost ------------------------------------------------------------------

FREE_SUBMISSION = "FREE_SUBMISSION"
PAID_CONSIDERATION_COST = "PAID_CONSIDERATION"
ADVERTISING = "ADVERTISING_OR_SPONSORSHIP"
SERVICE_FEE = "SERVICE_FEE"
GUARANTEED_PLACEMENT_COST = "GUARANTEED_PLACEMENT"
GUARANTEED_STREAMS_COST = "GUARANTEED_STREAMS"
COST_UNKNOWN = "UNKNOWN"

_MONEY_RE = re.compile(r"(?:(\$|€|£)\s?(\d+(?:[.,]\d{2})?)|(\d+(?:[.,]\d{2})?)\s?(USD|EUR|GBP))", re.I)
_CURRENCY_BY_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP"}

# --- role-based submission addresses --------------------------------------

ROLE_LOCAL_PARTS = {
    "submissions", "submission", "submit", "music", "demos", "demo", "promo",
    "promos", "programming", "editorial", "editor", "press", "info", "contact",
    "hello", "bookings", "radio", "playlist", "playlists", "curator", "team",
    "media", "pr", "partnerships", "business",
}

# --- genre vocabulary ------------------------------------------------------

GENRE_VOCABULARY = [
    "electronic", "dark electronic", "industrial", "techno", "house", "deep house",
    "tech house", "trance", "drum and bass", "dnb", "dubstep", "ambient", "downtempo",
    "synthwave", "darkwave", "coldwave", "ebm", "idm", "breakbeat", "garage",
    "alternative", "indie", "indie rock", "post-punk", "shoegaze", "dream pop",
    "rock", "metal", "punk", "hardcore", "pop", "synthpop", "electropop", "hyperpop",
    "hip hop", "rap", "trap", "r&b", "soul", "funk", "disco", "jazz", "lo-fi",
    "folk", "americana", "country", "classical", "experimental", "noise",
    "reggae", "dancehall", "afrobeats", "amapiano", "latin", "reggaeton",
]

_LOGIN_PATTERNS = [
    re.compile(r"\b(?:log\s?in|sign\s?in|create\s+an?\s+account|members?\s+only|dashboard)\b", re.I),
    re.compile(r"\byou\s+must\s+be\s+(?:logged\s+in|a\s+member)\b", re.I),
]
_CAPTCHA_PATTERNS = [
    re.compile(r"\b(?:captcha|recaptcha|hcaptcha|prove\s+you.{0,10}re\s+(?:not\s+a\s+robot|human))\b", re.I),
]

# --- page state -------------------------------------------------------------
#
# The first real discovery run qualified eight Cloudflare interstitials as
# curators named "Just a moment...", one 502 error page, and one page named
# "Cloudflare". A challenge page is not the site behind it: nothing on it is
# evidence about the outlet, so nothing may be classified, named or qualified
# from one.

PAGE_OK = "OK"
PAGE_BOT_CHALLENGE = "BOT_CHALLENGE"
PAGE_ERROR = "ERROR"

_CHALLENGE_PATTERNS = [
    re.compile(r"\bjust a moment\b", re.I),
    re.compile(r"\bchecking your browser\b", re.I),
    re.compile(r"\bverify(?:ing)? (?:that )?you are (?:a )?human\b", re.I),
    re.compile(r"\benable javascript and cookies to continue\b", re.I),
    re.compile(r"\battention required!?\s*\|\s*cloudflare\b", re.I),
    re.compile(r"\bddos protection by\b", re.I),
    re.compile(r"\bperformance & security by cloudflare\b", re.I),
    re.compile(r"\bchecking if the site connection is secure\b", re.I),
]
_ERROR_TITLE_RE = re.compile(
    r"^\s*(?:40[134]|429|50[0-9])\b|"
    r"\b(?:bad gateway|service unavailable|gateway time-?out|"
    r"access denied|page not found|too many requests)\b", re.I)


def page_state(sanitized, http_status=None):
    """Real content, or an interstitial standing in front of it?"""
    if http_status is not None and http_status >= 400:
        return PAGE_ERROR
    title = sanitized.get("title") or ""
    head = " ".join(filter(None, [title, sanitized.get("visible_text") or ""]))[:4000]
    for pattern in _CHALLENGE_PATTERNS:
        if pattern.search(head):
            return PAGE_BOT_CHALLENGE
    if _ERROR_TITLE_RE.search(title):
        return PAGE_ERROR
    return PAGE_OK


_DATE_RE = re.compile(
    r"\b(20\d{2}-\d{2}-\d{2})\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+(20\d{2})\b",
    re.I,
)


def _excerpt_around(text, match, width=160):
    start = max(0, match.start() - width // 2)
    end = min(len(text), match.end() + width // 2)
    return ("…" if start > 0 else "") + " ".join(text[start:end].split()) + ("…" if end < len(text) else "")


def classify(sanitized):
    """Highest-weight matching class wins; risk classes outrank everything.

    A match in the page *title* earns a bonus: "Decibel Magazine" mentioning a
    playlist somewhere in its body is a publication, not a playlist, and the
    title is where a site says what it is.
    """
    title = sanitized.get("title") or ""
    text = " ".join(filter(None, [
        title,
        sanitized.get("meta_description") or "",
        sanitized.get("visible_text") or "",
    ]))
    best = (UNKNOWN, 0, None)
    hits = []
    for classification, weight, pattern in _CLASS_RULES:
        match = pattern.search(text)
        if match:
            effective = weight + (20 if pattern.search(title) else 0)
            hits.append({
                "classification": classification,
                "weight": effective,
                "excerpt": _excerpt_around(text, match),
            })
            if effective > best[1]:
                best = (classification, effective, _excerpt_around(text, match))
    return {
        "classification": best[0],
        "confidence": min(0.95, best[1] / 100) if best[1] else 0.0,
        "excerpt": best[2],
        "all_matches": hits,
    }


def submission_state(sanitized):
    text = sanitized.get("visible_text") or ""
    for pattern in _CLOSED_PATTERNS:
        match = pattern.search(text)
        if match:
            return {"state": CLOSED, "confidence": 0.85, "excerpt": _excerpt_around(text, match)}
    for pattern in _OPEN_PATTERNS:
        match = pattern.search(text)
        if match:
            return {"state": OPEN, "confidence": 0.8, "excerpt": _excerpt_around(text, match)}
    return {"state": SUBMISSIONS_UNKNOWN, "confidence": 0.0, "excerpt": None}


def cost_model(sanitized, classification):
    text = sanitized.get("visible_text") or ""
    if classification == GUARANTEED_STREAMS:
        return {"model": GUARANTEED_STREAMS_COST, "amount": None, "currency": None,
                "excerpt": None, "confidence": 0.9}
    if classification == GUARANTEED_PLACEMENT:
        return {"model": GUARANTEED_PLACEMENT_COST, "amount": None, "currency": None,
                "excerpt": None, "confidence": 0.9}

    # "There is no submission fee" must not read as paid consideration, so the
    # free signal is tested before the fee signal.
    free_match = re.search(
        r"\b(?:free\s+to\s+submit|no\s+(?:submission\s+|review\s+)?fee|"
        r"submissions?\s+are\s+free|without\s+(?:a\s+)?fee)\b", text, re.I)
    if free_match:
        return {"model": FREE_SUBMISSION, "amount": 0.0, "currency": None,
                "excerpt": _excerpt_around(text, free_match), "confidence": 0.8}

    fee_match = re.search(r"\b(?:submission|review|consideration)\s+fee\b[^.\n]{0,60}", text, re.I)
    money = _MONEY_RE.search(fee_match.group(0)) if fee_match else None
    if fee_match:
        amount, currency = _parse_money(money) if money else (None, None)
        return {"model": PAID_CONSIDERATION_COST, "amount": amount, "currency": currency,
                "excerpt": _excerpt_around(text, fee_match), "confidence": 0.8}

    # Only an actual offer to sell placement counts. A page merely stating that
    # advertising is handled elsewhere is not a paid submission route.
    ad_match = re.search(
        r"\b(?:advertising|sponsorship)\s+(?:rates|packages|pricing|opportunities|"
        r"enquiries|inquiries)\b|\b(?:buy|purchase|book)\s+(?:a\s+)?"
        r"(?:sponsored\s+(?:post|content)|ad\b)", text, re.I)
    if ad_match:
        return {"model": ADVERTISING, "amount": None, "currency": None,
                "excerpt": _excerpt_around(text, ad_match), "confidence": 0.6}

    return {"model": COST_UNKNOWN, "amount": None, "currency": None,
            "excerpt": None, "confidence": 0.0}


def _parse_money(match):
    if match is None:
        return None, None
    symbol, symbol_amount, plain_amount, code = match.groups()
    raw = symbol_amount or plain_amount
    if raw is None:
        return None, None
    try:
        amount = float(raw.replace(",", "."))
    except ValueError:
        return None, None
    currency = _CURRENCY_BY_SYMBOL.get(symbol) if symbol else (code.upper() if code else None)
    return amount, currency


def genres(sanitized):
    text = (" ".join(filter(None, [
        sanitized.get("title") or "",
        sanitized.get("meta_description") or "",
        sanitized.get("visible_text") or "",
    ]))).lower()
    found = []
    for genre in GENRE_VOCABULARY:
        if re.search(r"\b" + re.escape(genre) + r"\b", text):
            found.append(genre)
    # Prefer the most specific match: "dark electronic" outranks "electronic".
    specific = [g for g in found if " " in g or "-" in g]
    broad = [g for g in found if g not in specific and
             not any(g in s.split() for s in specific)]
    return specific + broad


def contact_candidates(sanitized):
    """Email addresses with the evidence of where each one was published."""
    candidates = []
    for entry in sanitized.get("emails", []):
        value = entry["value"].strip()
        local = value.split("@")[0].lower()
        candidates.append({
            "value": value,
            "origin": entry["origin"],
            "role_based": local in ROLE_LOCAL_PARTS,
            "link_text": entry.get("text"),
        })
    # Deterministic order: role addresses first, then mailto links, then prose.
    candidates.sort(key=lambda c: (not c["role_based"], c["origin"] != "mailto-link", c["value"]))
    return candidates


def submission_links(sanitized):
    """Links whose text or href reads like a submission destination."""
    keywords = ("submit", "submission", "demo", "promo", "contact", "music",
                "pitch", "send", "upload")
    results = []
    for link in sanitized.get("links", []):
        href = link.get("href") or ""
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "data:")):
            continue
        haystack = f"{href} {link.get('text') or ''}".lower()
        if any(word in haystack for word in keywords):
            results.append({"href": href, "text": link.get("text")})
    return results[:10]


def requires_login(sanitized):
    text = sanitized.get("visible_text") or ""
    return any(pattern.search(text) for pattern in _LOGIN_PATTERNS)


def requires_captcha(sanitized):
    text = " ".join([sanitized.get("visible_text") or "", " ".join(
        (link.get("href") or "") for link in sanitized.get("links", []))])
    return any(pattern.search(text) for pattern in _CAPTCHA_PATTERNS)


def freshness(sanitized):
    """Most recent date the page states about itself, if any."""
    hint = sanitized.get("last_modified_hint")
    if hint:
        return {"date": hint[:10], "source": "markup", "confidence": 0.8}
    text = sanitized.get("visible_text") or ""
    dates = []
    for match in _DATE_RE.finditer(text):
        iso, year = match.group(1), match.group(2)
        if iso:
            dates.append(iso)
        elif year:
            dates.append(year)
    if not dates:
        return {"date": None, "source": None, "confidence": 0.0}
    return {"date": max(dates), "source": "page-text", "confidence": 0.4}


def outlet_name(sanitized, fallback_domain):
    title = sanitized.get("title") or ""
    # Strip the common "Page — Site" pattern down to the site half.
    for separator in ("|", "—", "–", "-", "::", "»"):
        if separator in title:
            parts = [part.strip() for part in title.split(separator) if part.strip()]
            if len(parts) >= 2:
                title = parts[-1]
                break
    title = title.strip()
    if 2 <= len(title) <= 80:
        return title
    return fallback_domain


def extract(sanitized, url, domain, http_status=None):
    """The single structured record produced from one page."""
    state = page_state(sanitized, http_status=http_status)
    classification = classify(sanitized)
    submission = submission_state(sanitized)
    cost = cost_model(sanitized, classification["classification"])
    return {
        "extractor_version": EXTRACTOR_VERSION,
        "sanitizer_version": sanitized.get("version", sanitizer.SANITIZER_VERSION),
        "page_state": state,
        "url": url,
        "domain": domain,
        "title": sanitized.get("title"),
        "outlet_name": outlet_name(sanitized, domain),
        "description": sanitized.get("meta_description"),
        "classification": classification["classification"],
        "classification_confidence": classification["confidence"],
        "classification_excerpt": classification["excerpt"],
        "classification_matches": classification["all_matches"],
        "submissions_open": submission["state"],
        "submissions_excerpt": submission["excerpt"],
        "submissions_confidence": submission["confidence"],
        "cost_model": cost["model"],
        "cost_amount": cost["amount"],
        "cost_currency": cost["currency"],
        "cost_excerpt": cost["excerpt"],
        "genres": genres(sanitized),
        "contacts": contact_candidates(sanitized),
        "hidden_emails": sanitized.get("hidden_emails", []),
        "submission_links": submission_links(sanitized),
        "requires_login": requires_login(sanitized),
        "requires_captcha": requires_captcha(sanitized),
        "freshness": freshness(sanitized),
        "injection_findings": sanitized.get("injection_findings", []),
        "forms": sanitized.get("forms", []),
    }
