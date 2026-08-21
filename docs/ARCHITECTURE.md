# REACH — Architecture

## 0. Repository audit (before any code was written)

REACH was first built as a module inside the Royalty Sweep app in this
repository, on the reasoning that a native module beats a disconnected one. That
was reversed on the product owner's instruction: REACH is its own product with
its own lifecycle and had to stop sharing a process, a catalog and a deployment
with anything else. The audit below is what the surrounding repository looked
like, and it is why several decisions were made the way they were — REACH kept
the Flask/Jinja/Tailwind stack and the design tokens because they were already
proven here, and it kept its own SQLite store because it always had one.

| Question | Finding |
| --- | --- |
| Sibling product | **Royalty Sweep by Street Banker** — where REACH first lived |
| Framework | Flask 3 + Jinja2, Python 3.11/3.12 |
| Front end | Tailwind via CDN, Chart.js via CDN, server-rendered Jinja. No build step, no `package.json` for this app. **REACH does not use the CDN** — see below |
| Database | **None** in the sibling app: `royalty_data.py` holds module-level seeded dataclasses. REACH brought its own |
| Authentication | **None.** No sessions, no users, no login |
| Tenancy | **None.** Single implicit account |
| Queue / async | **None.** Everything happens inside the request |
| Email / AI / storage / analytics infra | **None** |
| Design system | Dark shell: `#0a0a0a` page, `#111113` panels, `border-white/5` hairlines, `amber-500` accent, `uppercase tracking-[0.2em]` section labels, `rounded-xl`/`rounded-2xl` cards |
| Tests | pytest, 109 passing in the sibling app at the start of this work, and still passing untouched |
| CI | `.github/workflows/ci.yml` — one job per product; REACH added its own |
| Unrelated directories | `mx-lab/` (TRACE, a motocross telemetry product) and `fuel-map-tool/` share the repo but are separate products. **Untouched.** |
| Deployment | The root `render.yaml` deploys TRACE only. REACH has its own blueprint in `reach-app/render.yaml` and does not touch it |

### Conflicts between the specification and the codebase, and how each was resolved

The brief assumes a production application with a relational database, auth, tenancy,
a queue, an email provider and LLM infrastructure, and its interface snippets are
TypeScript. None of that existed. Every resolution below favours the existing
codebase over the brief's implied stack, as the execution contract requires.

| Conflict | Resolution |
| --- | --- |
| "Use the existing relational database" — there is none | Introduced **SQLite through the standard library** (`reach/db.py`). A real relational store with foreign keys and versioned migrations, and **zero new runtime dependencies**. Phase One requires campaign state to persist and metrics to derive from records; in-memory dicts cannot do that. No graph database — the brief forbids one in Phase One and nothing here needs one. |
| TypeScript interfaces in the brief | Implemented as Python dataclasses and typed dicts with **the same field names** (`ProviderPolicy`, `SourceUsagePolicy`, `EvidencePacket`, `ComplianceDecisionRecord`), so the contracts match the specification exactly while staying idiomatic for this codebase. |
| "Do not create a second authentication system" — there is no first one | `reach/rbac.py` models roles and permissions properly and enforces them on every privileged call. The acting principal resolves from `REACH_PRINCIPAL_EMAIL` / `REACH_PRINCIPAL_ROLE`. When REACH gains real sessions, `rbac.current_principal()` is the **single** function that changes. |
| "Reuse the existing queue" — there is none | `reach/jobs.py` implements a durable queue over the same SQLite store: idempotency keys, retries with exponential backoff, cancellation, progress, dead-letter, per-provider concurrency, cost accounting. A background thread drains it in production; tests drain it synchronously. |
| Pitch writing implies an LLM; no credential exists | Drafts are composed **deterministically** from first-party facts and permitted evidence, with every sentence bound to a source in `facts_json`. The LLM adapter exists, reports `DISABLED`, and the firewall gates the composition path exactly as it would gate a model call. Nothing is fabricated. |
| Encryption at rest | Added `cryptography` to `requirements.txt` — one well-known dependency, used for Fernet. Without `REACH_ENCRYPTION_KEY` the process uses an ephemeral key and says so on Provider Health. Nothing is ever stored in plaintext. |
| Tailwind via `cdn.tailwindcss.com` | That script is a development tool by Tailwind's own documentation, and loading it makes every page render depend on a third party being reachable. REACH builds the stylesheet ahead of time into `static/tailwind.css` and serves it itself. Tailwind **v3** is pinned deliberately — it is what the CDN served while these screens were designed, so the switch changes nothing visually. The CSS is committed, so deploying stays `pip install` + `gunicorn` with no node step; `tools/build-css.sh` regenerates it when a template gains a class the build does not have. A test fails if any page reintroduces a third-party asset. |
| Search backend | Brave Search API and Google Programmable Search are implemented. Without a credential, discovery runs a **fixture corpus**, and every screen and job result is labelled `FIXTURE`. Claude's own web search is deliberately not the production backend. |

## 1. Where REACH lives

REACH is a standalone application in `reach-app/`. Nothing outside that
directory is imported, read or written at runtime.

```
music-artist-hub/
├── app.py                     # Royalty Sweep — a separate product, untouched
├── royalty_data.py            # its catalog — untouched
├── render.yaml                # deploys TRACE only — untouched
├── mx-lab/                    # TRACE — a separate product
├── fuel-map-tool/             # a separate product
└── reach-app/                 # ← REACH, entirely self-contained
    ├── app.py                 # REACH's Flask entry point
    ├── requirements.txt       # REACH's dependencies
    ├── render.yaml            # REACH's own Render blueprint
    ├── Dockerfile             # REACH's own container
    ├── pyproject.toml         # REACH's own lint and test config
    ├── reach/                 # the package
    ├── static/
    │   └── tailwind.css       # built ahead of time, committed, served by REACH
    ├── tools/build-css.sh     # regenerates it; the only step that needs node
    ├── templates/
    │   ├── base.html          # REACH's shell
    │   └── reach/             # 15 screens
    ├── tests/                 # 183 tests
    └── docs/                  # these eight documents
```

The blueprint keeps its `/reach` prefix now that it is its own app: every route,
bookmark, test and document already refers to it, and `/` redirects there.

REACH is not a separate application. Same Flask app, same Jinja environment, same
templates directory, same pytest suite, same CI job.

## 2. Module map

```
reach/
├── config.py        env-only configuration; nothing reaches the browser
├── db.py            SQLite store, 8 migrations, per-thread connections
├── crypto.py        Fernet at rest, keyed hashes for dedup and suppression
├── clock.py         one clock, freezable in tests
├── errors.py        typed failures; each one is a deliberate decision
├── rbac.py          roles, permissions, acting principal
├── audit.py         hash-chained append-only event log
│
├── policy.py        provider capability + policy registry, quota, kill switches
├── firewall.py      source-usage policy and the AI firewall
│
├── catalog.py       REACH's own recording catalog; rights attestation
├── profile.py       Track Intelligence Profile with per-field provenance
├── tracks.py        the Track record, the add-track mapping, the sample catalog
├── firstparty.py    first-party facts permitted into pitch copy
│
├── netguard.py      SSRF / DNS-rebinding / port / MIME / file-type guard
├── fetcher.py       robots-aware fetcher, pluggable transport
├── sanitizer.py     HTML → text + structured facts, injection detection
├── extractor.py     deterministic schema-constrained extraction
├── evidence.py      evidence packets, source documents, retention
├── entities.py      outlets, routes, deduplication, merge/undo
├── contacts.py      contact categories, encryption, suppression, sendability
│
├── scoring.py       REACH score and the independent risk score
├── compliance.py    deterministic communication decisions
├── campaigns.py     campaigns, targets, CRM statuses, budgets
├── jobs.py          durable job runner
├── pipeline.py      the nine discovery passes
│
├── drafts.py        source-grounded pitch composition
├── sender.py        sender identity and the sender-health gate
├── approvals.py     approval integrity and the send executor
├── humanactions.py  NEEDS YOU queue and DSP deadline engine
├── outcomes.py      responses and evidence-gated placements
├── relationships.py durable relationship intelligence
├── analytics.py     metrics derived from persisted rows
├── web.py           Flask blueprint
├── providers/       adapters: search, youtube, soundcloud, mixcloud,
│                    musicbrainz, listenbrainz, spotify, email
└── fixtures/        deterministic web corpus incl. adversarial pages
```

## 3. Agent architecture

Research and action are separated by construction, not by convention. The
"agents" in this build are deterministic components with the permission
boundaries the brief specifies:

| Role | Implementation | Can it act externally? |
| --- | --- | --- |
| Orchestrator | `jobs.py` + `pipeline.start` | Enqueues only; owns budgets and stop rules |
| Research Planner | `pipeline.plan_queries` | No. Pure function over the profile |
| Search/Crawl Worker | `providers/search.py`, `fetcher.py` | Read-only. No send path, no secrets beyond its own provider key |
| Extractor | `sanitizer.py` + `extractor.py` | No. Deterministic rules; page text is data, never instructions |
| Entity Resolution | `entities.py` | Merges only on exact deterministic rules; fuzzy matches are suggestions |
| Policy & Compliance | `policy.py`, `firewall.py`, `compliance.py` | Cannot override provider policy or suppression |
| Scoring | `scoring.py` | No send capability |
| Pitch Writer | `drafts.py` | Composes only; cannot send |
| Action Executor | `approvals.send_approved` | Executes one already-approved payload hash. No browsing, no rewriting, no re-addressing |
| Placement Monitor | `outcomes.py`, `providers/spotify.playlist_contains_track` | Records evidence; never converts a reply into a placement |

The Claude Agent SDK is not used as a production runtime, per the brief. If a
model is connected later, `firewall.guard_language_model` is already the gate
every payload must pass, and `sanitizer.as_data_block` is already the fence
untrusted text goes through.

## 4. Request and job flow

```
Browser
  └─ POST /reach/campaigns              campaigns.create  (rights + profile gates)
  └─ POST /reach/.../discover/start     pipeline.start → job_run rows
                                            │
        ┌───────────────────────────────────┘
        ▼
  PLAN_DISCOVERY ── SEARCH_PROVIDER ── FETCH_PUBLIC_PAGE ── RESOLVE_ENTITY
        │                                     │                    │
   query families              netguard + robots + budget    outlet/org/contact
                                              │              + evidence packets
                                              ▼                    │
                                        VERIFY_SUBMISSION_ROUTE ◄──┘
                                              │
                                        DEDUPLICATE
                                              │
                             SCORE_OPPORTUNITY → ASSESS_RISK → ASSESS_COMPLIANCE
                                              │
                              READY (direct route) │ QUALIFIED (+ human task)
                                              │
                          drafts.generate → approvals.approve → send_approved
                                              │
                        outcomes.record_response / record_placement
                                              │
                                    analytics (all derived)
```

Every arrow is a separate durable job row. A campaign can be paused, cancelled
and resumed at any arrow.

## 5. Design decisions worth stating

**One outlet table with a `kind` discriminator.** The brief lists Playlist,
Channel, Station, Blog, Publication, Podcast, DJProfile, DJPool and
CreatorProfile as separate entities. In Phase One their attribute sets are
identical, so they are one `outlet` table discriminated by `kind`, with
kind-specific extras in a validated `profile_json`. Organization, submission
route, contact and contact method remain separate tables — those genuinely
differ. Nine near-identical tables would have been ceremony, not normalization.

**Dedup keys on the contact route, not the outlet name.** The inbox is what
receives a message, so `contact:<keyed-hash>` is the primary key for
consolidation, with `domain:<registrable>` as the fallback for pages that carry
no route. This is what makes one curator's five playlists a single target.
Resolution always collapses onto the *earliest* target for a key — resolving
against "any other non-duplicate row" cascades, which cost a real bug during
this build and is now covered by a test.

**UNKNOWN is a first-class value.** Score components, profile fields, freshness,
follower counts and placement value all distinguish "not measured" from "zero".
`_overlap` returns `None`, not `0.0`, when either side is empty, and the weighted
score renormalizes over known components only.

**Fixture mode is labelled everywhere.** `AdapterResponse.mode` carries
`LIVE` or `FIXTURE` from the adapter to the job result to the screen header.

## 6. What is deliberately not built

Autopilot, automated form submission, CAPTCHA solving, authenticated browser
automation, social DMs, automated replies and autonomous spending all have
feature flags in `config.FEATURE_FLAGS`, are all `False`, and are all enforced
in code (`campaigns.create` refuses `AUTOPILOT`). They exist as switches so the
surrounding code has something real to test against — not as a soft launch.
