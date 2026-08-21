"""What a first-time operator still has to do, derived from real state.

This is not a tutorial. Every step below is answered by querying the database
or the process environment, so a step reads DONE because it *is* done and the
panel empties itself as the work happens. Nothing here can drift out of sync
with the product, because there is nothing here to keep in sync.

The second half is the more important one. REACH runs with no configuration at
all, which is a deliberate property — but running is not the same as being fully
capable, and a first-time operator has no way to tell which parts are inert. So
each unconfigured capability is named together with the consequence it has right
now, in the same voice the rest of the product uses for UNKNOWN: the gap is
shown, not filled in.
"""

import os

from . import campaigns, catalog, config, crypto, db, profile, rbac, sender
from .providers import search as search_provider

DONE = "DONE"
TODO = "TODO"

# Severity of an unconfigured capability, in the order an operator should care.
BLOCKING = "BLOCKING"      # data will be lost or outreach cannot happen
LIMITING = "LIMITING"      # REACH works, but on fixtures or with less reach


def _own_recordings(tenant_id):
    return db.query(
        "SELECT id FROM recording WHERE tenant_id = ? AND is_sample = 0", (tenant_id,))


def steps(tenant_id=None):
    """The path to a first real campaign, each step answered from the database."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    own = _own_recordings(tenant_id)
    recordings = catalog.recordings(tenant_id)
    campaign_rows = campaigns.list_campaigns(tenant_id)

    profiled = [r for r in recordings
                if profile.completeness(profile.get_or_create(r["id"]))["ready"]]
    attested = [r for r in recordings if catalog.has_rights_attestation(r["id"])]
    with_targets = [c for c in campaign_rows if campaigns.targets(c["id"])]
    approved = db.query_one(
        "SELECT a.id FROM approval a JOIN campaign_target t ON t.id = a.target_id "
        "JOIN campaign c ON c.id = t.campaign_id WHERE c.tenant_id = ? LIMIT 1", (tenant_id,))

    return [
        {"key": "track", "state": DONE if own else TODO,
         "label": "Add a track of your own",
         "detail": ("Five sample tracks are loaded so the pipeline can be tried straight away. "
                    "Their figures are illustrative — add a real recording before running a "
                    "campaign you intend to act on.")
                   if not own else f"{len(own)} of your own recordings in the catalog.",
         "href": "reach.catalog_view"},
        {"key": "profile", "state": DONE if profiled else TODO,
         "label": "Complete a track intelligence profile",
         "detail": ("Genre, mood, language and comparable artists cannot be derived from a "
                    "title, so REACH asks rather than guessing. A campaign will not launch "
                    "without the required fields.")
                   if not profiled else f"{len(profiled)} recordings have a complete profile.",
         "href": "reach.new_campaign"},
        {"key": "rights", "state": DONE if attested else TODO,
         "label": "Record the rights attestation",
         "detail": ("A hard gate: campaign creation is refused without one, because outreach "
                    "uses the master, the artwork and the artist's likeness.")
                   if not attested else f"{len(attested)} recordings are attested.",
         "href": "reach.new_campaign"},
        {"key": "campaign", "state": DONE if campaign_rows else TODO,
         "label": "Create a campaign",
         "detail": ("Scout researches only and can never send. Copilot adds approval-gated "
                    "email once the sending identity is healthy.")
                   if not campaign_rows else f"{len(campaign_rows)} campaigns.",
         "href": "reach.index"},
        {"key": "discovery", "state": DONE if with_targets else TODO,
         "label": "Run discovery",
         "detail": ("Nine passes across the sources this account is permitted to use. "
                    "Every opportunity it keeps carries the evidence behind it.")
                   if not with_targets else f"{len(with_targets)} campaigns have opportunities.",
         "href": "reach.index"},
        {"key": "approval", "state": DONE if approved else TODO,
         "label": "Review and approve a pitch",
         "detail": ("The review screen shows the exact payload and the source of every factual "
                    "claim. Nothing leaves REACH without passing through it.")
                   if not approved else "At least one pitch has been approved.",
         "href": "reach.index"},
    ]


def capabilities():
    """Unconfigured capabilities, each with the consequence it has right now."""
    items = []

    if crypto.key_state() == crypto.KEY_EPHEMERAL:
        items.append({
            "key": "encryption", "severity": BLOCKING,
            "label": "Encryption key not set",
            "detail": ("REACH is encrypting stored contacts with a key generated for this "
                       "process. Every record written now becomes unreadable when the process "
                       "restarts. Set REACH_ENCRYPTION_KEY before doing real work, and keep "
                       "it — rotating it has the same effect."),
            "fix": "REACH_ENCRYPTION_KEY"})

    if not os.environ.get(config.DB_PATH_ENV):
        # REACH cannot know whether a given path sits on a mounted volume, but
        # it can tell that nobody chose one. Unset means the store is beside the
        # application code, which on any hosted container is wiped whenever the
        # container is replaced — including when a free plan spins the service
        # down for inactivity, which is routine rather than rare.
        items.append({
            "key": "storage", "severity": BLOCKING,
            "label": "Storage is not durable",
            "detail": ("REACH_DB_PATH is not set, so the database sits next to the application "
                       "rather than on a disk that outlives it. If this is running on a hosted "
                       "container, everything goes when the container is replaced: the catalog, "
                       "campaigns, evidence, placements, the audit chain — and the suppression "
                       "list, which means someone who opted out stops being on record as having "
                       "opted out. Fine for trying REACH; not fine for outreach anyone receives."),
            "fix": "REACH_DB_PATH"})

    health = sender.health_summary()
    if not health["ready"]:
        items.append({
            "key": "sender", "severity": BLOCKING,
            "label": "Sending identity not ready",
            "detail": ("Drafts can be prepared, reviewed and approved, but REACH will not "
                       "send. Failing: " + ", ".join(health["failing"]) + "."),
            "fix": "See docs/INTEGRATION_SETUP.md"})

    if not search_provider.connected():
        items.append({
            "key": "search", "severity": LIMITING,
            "label": "No search credential",
            "detail": ("Discovery runs against the built-in fixture corpus of reserved "
                       ".example domains. The outlets it finds are test fixtures, not real "
                       "ones, and every screen labels the run as such."),
            "fix": "REACH_SEARCH_API_KEY"})

    missing = [name for name, env in config.PROVIDER_CREDENTIAL_ENV.items()
               if env and not config.credentials_present(name)
               and name not in ("email", "language_model", "open_web_search")]
    if missing:
        items.append({
            "key": "providers", "severity": LIMITING,
            "label": f"{len(missing)} discovery sources not connected",
            "detail": ("Discovery still runs, over fewer sources: "
                       + ", ".join(sorted(missing)) + ". Each one reads NOT CONNECTED on "
                       "the Connections screen rather than quietly returning nothing."),
            "fix": "reach.providers_view"})

    return items


def status(tenant_id=None):
    """Everything the first-run panel needs, and whether to show it at all."""
    step_rows = steps(tenant_id)
    capability_rows = capabilities()
    outstanding = [s for s in step_rows if s["state"] == TODO]
    return {
        "steps": step_rows,
        "capabilities": capability_rows,
        "outstanding": outstanding,
        "done_count": len(step_rows) - len(outstanding),
        "total": len(step_rows),
        "blocking": [c for c in capability_rows if c["severity"] == BLOCKING],
        # Once there is nothing left to do and nothing inert, the panel has no
        # reason to exist and stops rendering.
        "show": bool(outstanding or capability_rows),
    }
