"""Fixture search backend.

Ranks the fixture web corpus by term overlap so Scout has something real to walk
when no search credential is configured. Results are always tagged
``provider="fixture"`` and the UI labels the run as a fixture run.
"""

import re

from . import web

_STOPWORDS = {
    "the", "a", "an", "to", "for", "of", "and", "or", "in", "on", "with",
    "submit", "submissions", "music", "your",
}


def _tokens(text):
    return [t for t in re.findall(r"[a-z0-9&']+", (text or "").lower()) if t not in _STOPWORDS]


def _corpus():
    entries = []
    for url, page in web.PAGES.items():
        if url.endswith("/robots.txt"):
            continue
        body = page.get("body") or ""
        if not isinstance(body, str) or len(body) > 100_000:
            continue
        title_match = re.search(r"<title>(.*?)</title>", body, re.I | re.S)
        desc_match = re.search(r'name="description" content="(.*?)"', body, re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", body)
        entries.append({
            "url": url,
            "title": (title_match.group(1).strip() if title_match else url),
            "snippet": " ".join((desc_match.group(1) if desc_match else text).split())[:220],
            "haystack": " ".join([
                title_match.group(1) if title_match else "",
                desc_match.group(1) if desc_match else "",
                text,
            ]).lower(),
        })
    return entries


_CORPUS = None


def search(query, limit=10):
    global _CORPUS
    if _CORPUS is None:
        _CORPUS = _corpus()

    terms = _tokens(query)
    if not terms:
        return []
    scored = []
    for entry in _CORPUS:
        score = sum(1 for term in terms if term in entry["haystack"])
        if score:
            scored.append((score, entry))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["url"]))
    results = []
    for rank, (score, entry) in enumerate(scored[:limit], start=1):
        results.append({
            "rank": rank,
            "url": entry["url"],
            "title": entry["title"],
            "snippet": entry["snippet"],
            "score": score,
        })
    return results
