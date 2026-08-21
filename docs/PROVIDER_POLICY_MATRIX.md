# REACH — Provider Capability and Policy Matrix

The authoritative, machine-readable records live in
`reach/policy.py :: BASELINE_POLICIES` and are persisted to `provider_policy`.
This document mirrors them. **The code is the source of truth**; if the two ever
disagree, the code wins and this file is wrong.

Baseline snapshot date: **16 August 2026**. Next review: **16 November 2026**.
This is a snapshot, not permanent truth — reverify official documentation before
enabling any connector in production. Provider Health flags reviews that come
due, and `policy.mark_reviewed` records the reverification.

## Status vocabulary

`ACTIVE` · `LIMITED` · `MANUAL_ONLY` · `APPROVAL_REQUIRED` · `BETA` · `DISABLED` · `BLOCKED`

Only `ACTIVE`, `LIMITED` and `BETA` may be called at all. `policy.require_capability`
raises `PolicyViolation` for everything else, and an unregistered provider
resolves to `BLOCKED` by default.

## Matrix

| Provider | Status | Capabilities | LLM | Embed | Contacts | Retention | Human action |
| --- | --- | --- | :-: | :-: | :-: | --- | :-: |
| REACH catalog | ACTIVE | catalog search, analytics import | ✅ | ✅ | ❌ | — | — |
| Open Web Search | LIMITED | playlist / curator / profile discovery | ✅ | ✅ | ✅ | 180d | — |
| Open Web Fetch | LIMITED | profile discovery, contact resolution, submission form | ✅ | ❌ | ✅ | 180d | — |
| YouTube Data API | LIMITED | catalog, playlist, curator, profile, placement monitoring | ❌ | ❌ | ❌ | 30d | — |
| SoundCloud | LIMITED | catalog, playlist, curator, profile | ❌ | ❌ | ❌ | 30d | — |
| Mixcloud | LIMITED | catalog, curator, profile | ❌ | ❌ | ❌ | 30d | — |
| MusicBrainz | ACTIVE | catalog search | ✅ | ✅ | ❌ | — | — |
| ListenBrainz | LIMITED | catalog search, analytics import | ❌ | ❌ | ❌ | 90d | — |
| **Spotify** | LIMITED | catalog, authenticated read, editorial pitch, placement monitoring | ❌ | ❌ | ❌ | 30d | ✅ |
| Apple Music | MANUAL_ONLY | catalog, editorial pitch | ❌ | ❌ | ❌ | 30d | ✅ |
| Amazon Music | MANUAL_ONLY | editorial pitch | ❌ | ❌ | ❌ | — | ✅ |
| Pandora AMP | MANUAL_ONLY | editorial pitch, submission form | ❌ | ❌ | ❌ | — | ✅ |
| Audiomack | MANUAL_ONLY | editorial pitch, submission form | ❌ | ❌ | ❌ | — | ✅ |
| TIDAL | DISABLED | catalog search | ❌ | ❌ | ❌ | — | — |
| Deezer | DISABLED | catalog search | ❌ | ❌ | ❌ | — | — |
| Bandcamp | MANUAL_ONLY | profile discovery | ✅ | ❌ | ✅ | 180d | ✅ |
| TikTok | APPROVAL_REQUIRED | profile discovery | ❌ | ❌ | ❌ | — | ✅ |
| Instagram / Meta | APPROVAL_REQUIRED | profile discovery | ❌ | ❌ | ❌ | — | ✅ |
| Email provider | LIMITED | authenticated write | ❌ | ❌ | ❌ | — | — |
| Language model | DISABLED | — | ❌ | ❌ | ❌ | — | — |
| Chartmetric, Soundcharts, Viberate, Songstats | APPROVAL_REQUIRED | — | ❌ | ❌ | ❌ | — | ✅ |
| SubmitHub, Groover, Playlist Push, SoundCampaign | APPROVAL_REQUIRED | — | ❌ | ❌ | ❌ | — | ✅ |
| DJ pools, college-radio DB, sync DB | APPROVAL_REQUIRED | — | ❌ | ❌ | ❌ | — | ✅ |

## Spotify — the hard rules

Enforced in `reach/firewall.py` and `reach/contacts.py`, and covered by tests:

1. No Spotify content enters a language model. `firewall.guard_language_model(["spotify"])` raises.
2. No embeddings from Spotify content. `firewall.guard_embedding(["spotify"])` raises.
3. No model training on Spotify content.
4. An address found only through Spotify is **never** an outreach route.
   `contacts.classify(..., provider="spotify")` returns `SPOTIFY_ONLY_SOURCE`,
   which is not in `SENDABLE_CATEGORIES`, and `compliance.decide` returns `BLOCK`.
5. No crawling or scraping of Spotify surfaces — `webDiscoveryAllowed = False`,
   and the fetcher is never pointed at Spotify by the pipeline.
6. Retention is 30 days; `evidence.purge_expired` enforces it.
7. `disconnectDeletionRequired = True`; `evidence.delete_provider_data("spotify")` implements it.
8. Attribution and links are preserved on every Spotify-derived record.
9. Rate limits and `Retry-After` are honoured (`providers/base.get_json` →
   `ProviderRateLimited`, which the job runner backs off on).
10. Development Mode vs Extended Quota is displayed verbatim from
    `REACH_SPOTIFY_QUOTA_MODE`, defaulting to the honest `DEVELOPMENT_MODE`.

Spotify may be used as a user-authorized metadata connector, a link and
identifier source, a campaign destination, a human-action editorial-pitch
destination, and a placement-link source. It is not the contact-harvesting
engine. When a Spotify playlist looks relevant, the curator must be resolved
independently through a separate public professional source before any direct
email becomes eligible.

## YouTube quota accounting

`policy.QUOTA_UNIT_COST` prices each operation before it runs:
`search` = 100 units, `channels.list` / `playlists.list` / `playlistItems.list` /
`videos.list` = 1 unit, against a 10,000-unit daily default.
`policy.check_quota` warns at 80% and raises `QuotaExceeded` rather than
overspending. The API exposes no private creator contact information, and this
adapter never claims otherwise — `get_channel` returns an explicit
`contact_email: None`.

## Open web

Permitted only for publicly accessible pages, and only through
`reach/fetcher.py`, which enforces robots directives and crawl-delay, per-domain
budgets, redirect limits, response-size caps and a MIME allowlist, and identifies
itself honestly as `ReachBot/1.0`. Excerpts and hashes are stored; full pages are
not retained.

## Third-party commercial platforms

Never crawled. Integration is permitted only through a licensed API, an
authorized export, an affiliate/deep link or a contractual data arrangement.
All such providers sit at `APPROVAL_REQUIRED` with `commercialApprovalRequired = True`
and zero capabilities, so `require_capability` refuses every call.

## Controls available on Provider Health (`/reach/providers`)

Connection state · capability list · credentials present or missing · quota usage
and percentage · last successful request · last error · last policy review and
next review date · retention requirement · AI-use restrictions · contact-resolution
restriction · attribution requirement · per-provider kill switch · global emergency
stop · encryption-key state.
