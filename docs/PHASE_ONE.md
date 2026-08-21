# REACH — Phase One

REACH is a standalone application. It turns a track in its own catalog into a
researched, evidence-backed set of promotional opportunities, and routes each
one through the correct submission method — direct email where a published route
has been independently verified, a structured human task everywhere else.

## What a real user can do today

Opening a fresh install shows a **Getting started** panel: six steps to a first
real campaign, each answered from the database rather than from a checklist, and
every capability that is not configured yet named with the consequence it has
right now — an ephemeral encryption key, an unhealthy sending identity, fixture
mode, uncredentialed discovery sources. It empties itself as the work happens.


1. Open `/reach/catalog`, add a track — only a title and an artist are required
   — then pick it. A first run seeds five clearly badged SAMPLE tracks so the
   pipeline can be tried before real ones are entered.
2. Complete the Track Intelligence Profile. Fields REACH cannot derive stay
   UNKNOWN; the required ones must be supplied before a campaign can launch.
3. Record the rights attestation. Without it, campaign creation is refused.
4. Choose mode, territories, channels and budgets, then create the campaign.
5. Run a **Scout** campaign — read-only research, nine passes, durable jobs.
6. Watch progress, planned query families, agent activity and the dead-letter
   queue on the Discover screen.
7. Review qualified opportunities with REACH score, risk score and compliance
   decision on Opportunities.
8. Open any opportunity and read **Why do we know this?** — source URL, excerpt,
   retrieval date, verification date, confidence and corroborating domains.
9. Generate a source-grounded pitch. Every factual sentence lists its basis.
10. Approve an eligible direct-email submission in the Review screen, which
    shows the exact recipient, subject, body, links, cost, sending identity,
    compliance state and payload hash.
11. Send the approved payload. Any material edit invalidates the approval.
12. Work login-required, CAPTCHA-protected and DSP editorial routes through the
    **NEEDS YOU** queue, with copy-ready field answers.
13. Record responses, and record placements — which require an evidence source.
14. Read campaign metrics that are counts of persisted rows.

## Definition of done — status

| Requirement | Status | Where |
| --- | --- | --- |
| REACH is a standalone application | Done | `reach-app/` — own entry point, shell, store, catalog, deploy config and CI job |
| A real track can launch a campaign | Done | `campaigns.create` over `catalog.recordings()` |
| Scout performs permitted discovery | Done | `pipeline.py`, live search when credentialed, fixture corpus otherwise (labelled) |
| Every opportunity has provenance | Done | `evidence.py`; the detail screen renders it |
| Provider policies enforced in code | Done | `policy.require_capability`, not comments |
| Spotify content never reaches a model | Done | `firewall.guard_language_model`; tested |
| Spotify-only contacts cannot be emailed | Done | `contacts.classify` + `compliance.decide` → BLOCK; tested |
| Contact routes independently verified | Done | `contacts.sendability`, evidence-domain counting |
| Duplicate contacts controlled | Done | route-based dedup keys; five playlists → one target; tested |
| Sender-health gate works | Done | `sender.run_checks`; UNKNOWN fails the gate |
| Suppression works | Done | global + tenant, keyed hashes, never removed |
| Human approval works | Done | `approvals.approve` |
| Approval payload integrity works | Done | payload hash re-derived at send; tested |
| Human Action tasks work | Done | `humanactions.py`, NEEDS YOU screen |
| One email path works end to end | Done | `providers/email.py`; recording transport in tests, HTTP in production |
| Campaign statuses persist | Done | SQLite `campaign_target.status` |
| Placements require evidence | Done | NOT NULL FK + validation; tested |
| Metrics derived from real records | Done | `analytics.campaign_metrics`; asserted against raw SQL |
| Security tests pass | Done | 40 tests in `test_security.py` |
| Production build passes | Done | `pytest -q` → 270 passed |
| Interface polished and usable | Done | 14 screens on the host design system |
| Unavailable providers labelled honestly | Done | Provider Health shows NOT CONNECTED / MANUAL ONLY / APPROVAL REQUIRED |
| No major screen is a shell | Done | every screen renders persisted data; asserted in `test_web.py` |
| No metric invented | Done | UNKNOWN is a first-class value throughout |
| No unsupported automation presented as working | Done | Phase One non-goals are flags set to `False`, enforced in code |

## Phase One non-goals — all off and enforced

`reach.autopilot`, `reach.automated_form_submission`, `reach.captcha_solving`,
`reach.authenticated_browser_automation`, `reach.social_dm_automation`,
`reach.automated_reply_sending`, `reach.autonomous_spend`,
`reach.contact_graph_export`, `reach.model_training_on_campaign_data`.

`campaigns.create` refuses `AUTOPILOT` outright. Guaranteed streams and
guaranteed placement are blocked by the risk and compliance engines, not merely
discouraged.

## What is honestly not finished

* **Live provider calls are unexercised.** Every credentialed adapter
  (search, YouTube, SoundCloud, Mixcloud, MusicBrainz, ListenBrainz, Spotify,
  email) has its request construction, response parsing, quota accounting and
  error handling implemented and unit-tested against transports — but no real
  API key existed in this environment, so none has been run against the live
  service. Treat first production use as a verification step.
* **DNS is verified, with one caveat.** SPF, DKIM and DMARC are now looked up
  directly. A restricted network that cannot resolve them reports UNKNOWN and
  fails the gate; the operator-declared fallback covers that case and labels
  itself as declared. What REACH still cannot check is whether your provider is
  actually *included* in the SPF policy it read.
* **Placement monitoring is manual plus one API path.** Spotify playlist
  membership can be checked when credentialed; other outlets rely on recorded
  evidence.
* **Outcome metrics for placement value** are recorded only when supplied;
  `placement_value` reports `UNMEASURED` rather than estimating.

See `docs/INTEGRATION_SETUP.md` for the exact credential or approval each
provider needs.
