#!/usr/bin/env python3
"""Build a self-contained, click-through capture of every REACH screen.

The output is one HTML file: the application's own rendered markup and its own
stylesheet, frozen after a full campaign lifecycle. Not screenshots, not a
redraw — what the running app served. Useful for review on a phone, or for
showing the product to someone who cannot run it.

    python tools/build-walkthrough.py            # writes docs/preview.html
    python tools/build-walkthrough.py out.html

Each screen is mounted in its own shadow root, so the app's CSS reset cannot
leak into the surrounding page or the page's design into the app. Scripts are
stripped and every control is inert; the page says so at the top, along with the
three things about the capture that would otherwise mislead — it ran on the
fixture corpus, its sending domain is a reserved .example one, and its tracks
are the seeded samples.

Needs nothing but the app itself. No browser, no network.
"""

import html as html_lib
import os
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SHELL = ROOT / "tools" / "walkthrough-shell.html"

# What each screen is for, and what is worth looking at on it. This is the only
# hand-written content in the output.
NOTES = {
    "catalog": ("REACH's own tracks — not another app's",
        "Only a title and an artist are required to add one. The chips under each track are release "
        "readiness, and each states its consequence: a missing ISRC blocks different routes than a "
        "missing UPC. The five seeded tracks carry a SAMPLE badge because their figures are "
        "illustrative, not measured."),
    "wizard": ("The profile a campaign cannot launch without",
        "Fields REACH cannot derive stay UNKNOWN rather than being guessed from the title. Local audio "
        "analysis reports that it has no audio source instead of simulating one. The rights attestation "
        "at the bottom is a hard gate — without it, campaign creation is refused."),
    "campaigns": ("Every campaign, with counts that are queries",
        "Discovered, high fit, verified, ready, submitted, placed. Each number is a count of rows in the "
        "database at the moment the page rendered, not a cached summary that can drift away from what "
        "actually happened. On a fresh install this screen also carries the Getting started panel."),
    "overview": ("Campaign health",
        "The scoreboard for one campaign, plus what it is waiting on. Health is derived from the same "
        "records the rest of the screens read."),
    "discover": ("Nine passes, and what each one asked",
        "The planned query families are shown before they run — channel families, territory families and "
        "local-language families are interleaved so one kind cannot eat the whole budget. Failed jobs go "
        "to a dead-letter queue rather than disappearing."),
    "opportunities": ("Qualified, scored, and decided",
        "REACH score and risk score are separate numbers, and the compliance decision is a third. A high "
        "score never overrides a BLOCK — the guaranteed-streams and guaranteed-placement fixtures in this "
        "run were both blocked outright."),
    "opportunity": ("Why do we know this?",
        "Every claim on this screen carries a source URL, the excerpt it came from, when it was retrieved, "
        "when it was last verified and which independent domains corroborate it. Score components that are "
        "unknown say UNKNOWN — they are never counted as zero."),
    "review": ("Exactly what will be sent",
        "The recipient, subject, body, links, cost, sending identity and compliance state, plus a payload "
        "hash that is re-derived at send time. Change any material field and the approval is invalidated. "
        "Under the message, every factual sentence is listed with its basis."),
    "outreach": ("Sender health, checked in DNS",
        "SPF, DKIM, DMARC and MX are looked up and read, not declared. A record that is absent, one that "
        "is published but protects nothing, and a lookup that failed are three different states and are "
        "reported differently."),
    "responses": ("What came back",
        "Responses are recorded against the submission that caused them, so the funnel on every other "
        "screen stays a count of real events."),
    "placements": ("A placement needs evidence",
        "Acceptance is not placement. Recording one requires an evidence source, which is stored with it — "
        "so the placement count can always be traced back to something that was actually seen."),
    "needs-you": ("The work REACH will not automate",
        "Login walls, CAPTCHA-protected forms and DSP editorial pitches become structured human tasks with "
        "copy-ready answers. Each says why it is not automated, and opening the destination deliberately "
        "does not mark it submitted."),
    "providers": ("No provider ever shows a fake green",
        "Anything without a credential reads NOT CONNECTED. Spotify's restrictions are stated on the screen "
        "rather than buried in code: its content may not enter a language model, and an address found only "
        "through Spotify is never an outreach route."),
    "relationships": ("History per outlet",
        "Submissions, responses, acceptances, placements, last contact and when the outlet is next eligible "
        "— so a second approach is a decision rather than an accident."),
    "settings": ("Flags, stops, and the audit chain",
        "Feature flags for everything deliberately out of scope, an emergency send stop, per-provider kill "
        "switches, and an append-only audit log whose hash chain is verified on this screen. Tampering with "
        "a row breaks the chain and it says so."),
}

# The first eleven are the order a track actually moves through, which is why
# they are numbered. The last four are not a sequence and are not numbered.
PATH = ["catalog", "wizard", "campaigns", "overview", "discover", "opportunities",
        "opportunity", "review", "outreach", "responses", "placements"]
ALWAYS = ["needs-you", "providers", "relationships", "settings"]

LABELS = {
    "campaigns": "Campaigns", "catalog": "Catalog", "wizard": "New campaign",
    "overview": "Overview", "discover": "Discover", "opportunities": "Opportunities",
    "opportunity": "One opportunity", "review": "Review & approve", "outreach": "Outreach",
    "responses": "Responses", "placements": "Placements", "needs-you": "Needs You",
    "providers": "Provider health", "relationships": "Relationships", "settings": "Settings",
}


def capture():
    """Drive a full lifecycle, then read every screen back out of the app."""
    workdir = tempfile.mkdtemp(prefix="reach-walkthrough-")
    os.environ.update({
        "REACH_DB_PATH": os.path.join(workdir, "walkthrough.db"),
        "REACH_ENCRYPTION_KEY": "reach-walkthrough-key-0123456789",
        "REACH_SENDER_FROM": "reach@outreach.streetbanker.example",
        "REACH_SENDER_NAME": "Street Banker REACH",
        "REACH_SENDER_POSTAL_ADDRESS": "12 Example Street, Berlin, Germany",
        "REACH_SENDER_POSTAL": "12 Example Street, Berlin, Germany",
        "REACH_SENDER_COUNTRY": "DE",
        "REACH_EMAIL_API_KEY": "walkthrough",
        "REACH_EMAIL_WEBHOOK_SECRET": "walkthrough",
        "REACH_SENDER_DKIM_SELECTOR": "selector1",
        "REACH_SENDER_DOMAIN_VERIFIED": "1",
    })
    from reach import (analytics, approvals, campaigns, catalog, dns_checks, drafts,
                       fetcher, humanactions, outcomes, pipeline, policy, profile, rbac)
    from reach.providers import email as email_provider

    domain = "outreach.streetbanker.example"
    dns_checks.set_resolver(dns_checks.offline_resolver({
        (domain, "TXT"): ["v=spf1 include:provider.example -all"],
        (f"selector1._domainkey.{domain}", "TXT"): ["v=DKIM1; k=rsa; p=MIIBIjAN"],
        (f"_dmarc.{domain}", "TXT"): ["v=DMARC1; p=reject; rua=mailto:dmarc@streetbanker.example"],
    }))
    fetcher.set_transport(fetcher.FixtureTransport())
    email_provider.set_transport(email_provider.RecordingTransport())

    rbac.ensure_default_tenant()
    policy.seed_policies()
    catalog.ensure_catalog()
    recording = catalog.recording_by_slug("midnight-drive")
    profile_id = profile.get_or_create(recording["id"])
    for field, value in [("primary_genre", "dark electronic"),
                         ("microgenres", ["dark electronic", "industrial", "coldwave"]),
                         ("mood", ["brooding", "nocturnal"]), ("language", "English"),
                         ("comparable_artists", ["HEALTH", "Boy Harsher"])]:
        profile.set_field(profile_id, field, value)
    catalog.attest_rights(recording["id"])
    campaign_id = campaigns.create(recording["id"], mode=campaigns.COPILOT, territories=["DE"])
    pipeline.start(campaign_id)
    pipeline.run_to_completion(campaign_id)
    analytics.capture_baseline(campaign_id)
    humanactions.build_dsp_tasks(campaign_id)
    ready = campaigns.targets(campaign_id, status=campaigns.READY)
    target_id = (ready[0] if ready else campaigns.targets(campaign_id)[0])["id"]
    drafts.generate(target_id)
    approvals.approve(target_id)
    approvals.send_approved(target_id)
    outcomes.record_response(target_id, outcomes.ACCEPT)
    outcomes.record_placement(target_id, outcomes.EVIDENCE_URL,
                              url="https://bassforge.example/promo")

    from app import create_app
    client = create_app().test_client()

    routes = {
        "campaigns": "/reach", "catalog": "/reach/catalog",
        "wizard": f"/reach/new?recording_id={recording['id']}",
        "overview": f"/reach/campaigns/{campaign_id}",
        "discover": f"/reach/campaigns/{campaign_id}/discover",
        "opportunities": f"/reach/campaigns/{campaign_id}/opportunities",
        "opportunity": f"/reach/campaigns/{campaign_id}/targets/{target_id}",
        "review": f"/reach/campaigns/{campaign_id}/review",
        "outreach": f"/reach/campaigns/{campaign_id}/outreach",
        "responses": f"/reach/campaigns/{campaign_id}/responses",
        "placements": f"/reach/campaigns/{campaign_id}/placements",
        "needs-you": "/reach/needs-you", "providers": "/reach/providers",
        "relationships": "/reach/relationships", "settings": "/reach/settings",
    }

    screens, url_to_id = {}, {}
    for sid, path in routes.items():
        url_to_id[path.split("?")[0]] = sid
    for sid, path in routes.items():
        response = client.get(path)
        if response.status_code != 200:
            raise SystemExit(f"{path} returned {response.status_code}")
        body = response.get_data(as_text=True)
        body = body.split("<body", 1)[1].split(">", 1)[1].rsplit("</body>", 1)[0]
        screens[sid] = {"label": LABELS[sid], "path": path, "html": body}

    css = client.get("/static/tailwind.css").get_data(as_text=True)
    return screens, css, url_to_id


def clean(markup, url_to_id):
    """Strip the app's scripts and turn its links into screen jumps."""
    markup = re.sub(r"<script\b[^>]*>.*?</script>", "", markup, flags=re.S | re.I)

    def relink(match):
        quote, href = match.group(1), match.group(2)
        target = url_to_id.get(href.split("?")[0])
        if target:
            return f'href={quote}#{quote} data-goto={quote}{target}{quote}'
        return f'href={quote}#{quote} data-inert={quote}1{quote}'

    return re.sub(r'href=(["\'])([^"\']*)\1', relink, markup)


def build(screens, css, url_to_id):
    rail = ['<div class="rail-group"><p class="rail-head">'
            '<span class="long">The path a track takes</span>'
            '<span class="short">Pipeline</span></p>']

    def item(sid, index=None):
        num = (f'<span class="num">{index:02d}</span>' if index
               else '<span class="num dot">&middot;</span>')
        return (f'<button class="rail-item" data-goto="{sid}" type="button" role="tab" '
                f'aria-selected="false" aria-controls="panel-{sid}">{num}'
                f'<span class="rail-label">{html_lib.escape(screens[sid]["label"])}</span></button>')

    for index, sid in enumerate(PATH, 1):
        rail.append(item(sid, index))
    rail.append('</div><div class="rail-group"><p class="rail-head">'
                '<span class="long">Always available</span>'
                '<span class="short">Global</span></p>')
    for sid in ALWAYS:
        rail.append(item(sid))
    rail.append('</div>')

    panels = []
    for sid in PATH + ALWAYS:
        heading, note = NOTES[sid]
        panels.append(
            f'<section class="panel" id="panel-{sid}" data-screen="{sid}" role="tabpanel" hidden>'
            f'<header class="panel-head">'
            f'<p class="eyebrow">{html_lib.escape(screens[sid]["label"])}</p>'
            f'<h2>{html_lib.escape(heading)}</h2>'
            f'<p class="note">{html_lib.escape(note)}</p>'
            f'<p class="path"><span>route</span> <code>{html_lib.escape(screens[sid]["path"])}</code></p>'
            f'</header>'
            f'<div class="bezel"><div class="mount" data-mount="{sid}"></div></div>'
            f'</section>')

    templates = "".join(
        f'<template data-tpl="{sid}">{clean(screens[sid]["html"], url_to_id)}</template>'
        for sid in PATH + ALWAYS)

    page = SHELL.read_text()
    return (page.replace("<!--RAIL-->", "".join(rail))
                .replace("<!--PANELS-->", "".join(panels))
                .replace("<!--TEMPLATES-->", templates)
                .replace("/*APPCSS*/", css.replace("</", "<\\/")))


def main():
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "preview.html"
    screens, css, url_to_id = capture()
    page = build(screens, css, url_to_id)
    out.write_text(page)
    print(f"{out}: {len(screens)} screens, {len(page.encode()) // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
