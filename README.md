# REACH

Global music discovery, opportunity intelligence, outreach and submission
management. REACH finds the playlists, blogs, radio shows, labels and sync
outlets that plausibly fit a track, records the evidence behind every claim it
makes, and routes each opportunity either to an approved direct email or to a
structured human task.

**REACH is a standalone application.** It has its own entry point, its own
templates, its own database and its own recording catalog. It shares no process,
no data and no deployment with the other products in this repository.

## Run it

```bash
cd reach-app
pip install -r requirements.txt
python app.py
```

Then open `http://127.0.0.1:5000` — `/` redirects to `/reach`.

REACH needs no internet connection to render: the stylesheet is built ahead of
time into `static/` and served by the app, so there is no CDN in the page and
nothing to load from a third party.

REACH runs with **no configuration at all**. Without a search credential,
discovery runs against a built-in fixture corpus of reserved `.example` domains,
and every screen labels the run as fixture mode rather than passing invented
results off as real ones. See [`docs/INTEGRATION_SETUP.md`](docs/INTEGRATION_SETUP.md)
to connect real providers.

## First run

Opening REACH shows a **Getting started** panel: the six steps to a first real
campaign, and everything that is not configured yet stated with its consequence.
Both halves are derived from your actual data and environment, so the panel is
never out of date and disappears once there is nothing left to say.

The one worth acting on immediately is `REACH_ENCRYPTION_KEY`. Without it REACH
encrypts stored contacts with a key generated for that process, and every record
written becomes unreadable at the next restart. The Render blueprint generates
one for you; a local run needs it set by hand for work you intend to keep.

## Your catalog

The **Catalog** screen is where tracks come from. REACH does not read another
application's songs: you add a track, and only a title and an artist name are
required. Anything you leave blank stays UNKNOWN — REACH shows the gap instead
of filling it in, because a missing ISRC blocks different routes than a missing
UPC does, and a stream count you never entered is not a stream count of zero.

REACH is not a royalty system and has no reporting feed. Any performance figure
a pitch quotes is one you entered yourself, and it is labelled as such in the
"source of every factual claim" panel on the review screen.

A first run seeds five **sample** tracks so the pipeline can be exercised before
you enter real ones. Every one carries a SAMPLE badge; their figures are
illustrative, not measured. Delete them from the Catalog screen once your own
tracks are in, or start empty with `REACH_SEED_SAMPLE_TRACKS=0`.

## Deploy

**On Render, sync the blueprint at the repository root.** Render only reads
`render.yaml` from the root of a repo, so the root file is what declares the
`reach` service (alongside the unrelated `trace` one). The copy in this
directory is not what gets deployed while REACH lives here — see the note at the
top of it.

    New → Blueprint → this repository → Apply

Render generates `REACH_ENCRYPTION_KEY` itself. The only value it asks you for
is `REACH_PRINCIPAL_EMAIL`, the address REACH treats as the acting user until it
has logins.

**The blueprint provisions a paid starter instance with a mounted disk**, so the
SQLite store survives deploys, restarts and spin-downs. That durability is what
makes REACH fit for real use: the catalog, campaigns, evidence, placements, the
audit chain and — above all — the suppression list persist. Losing suppression
is the failure with a victim: a recipient who opted out stops being on record as
having opted out.

To trial REACH free instead, edit the `reach` service in the root `render.yaml`:
set `plan: free`, delete the `disk:` block and the `REACH_DB_PATH` env var.
Everything still runs, but the database dies with the container — REACH's start
screen reports this as a blocking item — so never send real outreach from a free
instance.

## Making discovery real

Without a search credential, discovery runs on the built-in fixture corpus and
says so on every screen. To search the real web, add one environment variable to
the service (Render → the `reach` service → Environment):

| Variable | Value |
| --- | --- |
| `REACH_SEARCH_API_KEY` | Your Brave Search API key (default backend) |

Google Programmable Search is also supported: set `REACH_SEARCH_BACKEND` to
`google_cse` and add `REACH_SEARCH_CSE_ID` alongside the API key. No code
change or redeploy config is needed for either — REACH reads the variable on
the next request, leaves fixture mode, and labels the run accordingly. Each
discovery run uses at most 24 search queries, budgeted in `reach/config.py`.

**Without a blueprint**, create a Web Service by hand:

| Field | Value |
| --- | --- |
| Root Directory | `reach-app` |
| Runtime | Python |
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn --bind 0.0.0.0:$PORT --workers 2 --timeout 120 app:app` |
| Health check path | `/healthz` |

Then set `REACH_ENCRYPTION_KEY` to any long random string, and point
`REACH_DB_PATH` at a mounted disk so the store survives a restart. `Dockerfile`
is the container equivalent of all of this.

## Develop

```bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check .
pytest
```

Both run in CI as the `REACH tests & lint` job.

`tools/build-walkthrough.py` rebuilds the click-through capture of every screen
(the app's own markup and stylesheet, frozen after a full campaign lifecycle) as
one self-contained HTML file. It needs no browser and no network.

`tools/audit-contrast.py` measures text contrast on every screen in a real
browser and exits non-zero if anything falls below WCAG AA. It is the authority;
`tests/test_a11y.py` is its CI-runnable proxy and runs on every commit.

`static/tailwind.css` is committed so deploying stays a pure-Python operation.
Run `tools/build-css.sh` after adding a utility class a template has not used
before — that is the only step that needs node, and a test tells you when it is
needed.

**Pre-release schema note.** REACH has never been deployed, so the migration
that creates the catalog was edited in place rather than layered over with an
ALTER. If you have a `reach.db` from an earlier local run, delete it — it
predates the standalone catalog and will not migrate forward.

## Documentation

[Phase One](docs/PHASE_ONE.md) ·
[Architecture](docs/ARCHITECTURE.md) ·
[Data model](docs/DATA_MODEL.md) ·
[Provider policy matrix](docs/PROVIDER_POLICY_MATRIX.md) ·
[Security threat model](docs/SECURITY_THREAT_MODEL.md) ·
[Outreach compliance](docs/OUTREACH_COMPLIANCE.md) ·
[Integration setup](docs/INTEGRATION_SETUP.md) ·
[Test plan](docs/TEST_PLAN.md)
