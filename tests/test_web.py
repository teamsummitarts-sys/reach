"""Web-layer tests: every REACH screen renders, and the honest-labelling rules
hold in the rendered HTML.

Covers Phase One scenarios 16, 33, 38 and the "no screen is a shell" bar.
"""

import pytest

from app import create_app
from reach import analytics, approvals, campaigns, catalog, drafts, humanactions, outcomes

from .conftest import first_ready_target


@pytest.fixture
def client():
    return create_app().test_client()


@pytest.fixture
def live_campaign(scouted, sender_ready):
    """A campaign with a full lifecycle: discovered, sent, answered, placed."""
    analytics.capture_baseline(scouted)
    humanactions.build_dsp_tasks(scouted)
    target = first_ready_target(scouted)
    drafts.generate(target["id"])
    approvals.approve(target["id"])
    approvals.send_approved(target["id"])
    outcomes.record_response(target["id"], outcomes.ACCEPT)
    outcomes.record_placement(target["id"], outcomes.EVIDENCE_URL,
                              url="https://bassforge.example/promo")
    return scouted


def page(client, path):
    response = client.get(path)
    assert response.status_code == 200, f"{path} returned {response.status_code}"
    return response.get_data(as_text=True)


# --- the standalone application --------------------------------------------

def test_root_serves_the_landing_page(client):
    body = page(client, "/")
    assert "Open REACH" in body
    assert 'href="/reach"' in body
    # The landing page sells the honesty mechanics, not invented results:
    # no fake counts, no testimonials — the components speak for themselves.
    assert "UNKNOWN" in body
    assert "What REACH refuses to do" in body
    assert "cdn.tailwindcss.com" not in body


def test_health_check_does_not_depend_on_the_database(client):
    assert client.get("/healthz").get_json() == {"ok": True, "service": "reach"}


def test_the_app_serves_reach_and_nothing_else(client):
    """REACH is its own application: no other product's routes are mounted."""
    rules = {rule.rule for rule in client.application.url_map.iter_rules()}
    stray = [rule for rule in rules
             if not rule.startswith("/reach") and rule not in ("/", "/healthz", "/static/<path:filename>")]
    assert stray == [], f"unexpected routes mounted: {stray}"


def test_the_page_loads_no_third_party_assets(client):
    """Every asset is served by REACH itself.

    A CDN link would make each page render depend on a third party being up, and
    cdn.tailwindcss.com is a development tool by its own documentation.
    """
    body = page(client, "/reach")
    head = body.split("<body")[0]
    assert "cdn.tailwindcss.com" not in body
    assert "//" not in head.replace("<!DOCTYPE", "").replace("http-equiv", "") \
        .replace('xmlns=\'http://www.w3.org/2000/svg\'', "")
    assert 'href="/static/tailwind.css' in body


def test_the_stylesheet_is_served_and_carries_the_design_tokens(client):
    response = client.get("/static/tailwind.css")
    assert response.status_code == 200
    css = response.get_data(as_text=True)
    for token in [r".bg-\[\#090909\]", r".bg-\[\#111113\]", r".bg-\[\#171719\]",
                  r".tracking-\[0\.2em\]"]:
        assert token in css, f"{token} missing from the build — rerun tools/build-css.sh"


def test_sending_is_not_offered_when_the_sender_is_not_ready(client, live_campaign):
    """A drafted, approved message with a sender whose DNS stops validating.

    Configuration alone must not re-enable the control: the records have to be
    published and readable, and when they stop being either, the screen says so.
    """
    from reach import dns_checks

    dns_checks.set_resolver(dns_checks.offline_resolver())  # every lookup now ABSENT
    dns_checks.clear_cache()
    body = page(client, f"/reach/campaigns/{live_campaign}/review")
    assert "Sending is disabled until the sender health checks pass." in body
    # No send action anywhere: the POST URL for sending ends in /send". The
    # nav's /reach/sender setup link is a page, not an action, and doesn't match.
    assert '/send"' not in body


def test_sending_is_offered_once_the_sender_is_healthy(client, live_campaign):
    body = page(client, f"/reach/campaigns/{live_campaign}/review")
    assert "Sending is disabled until the sender health checks pass." not in body
    assert "Send approved message" in body


def test_campaigns_screen_lists_catalog_tracks(client):
    """Starting a campaign from a track lives on the Campaigns screen now; the
    dashboard stays focused on what needs attention."""
    body = page(client, "/reach/campaigns")
    assert "Midnight Drive" in body
    assert "Neon Dreams" in body
    assert "Synthwave Surfer" in body


def test_catalog_screen_lists_tracks_and_labels_sample_data(client):
    body = page(client, "/reach/catalog")
    assert "Add a track" in body
    assert "Midnight Drive" in body
    # The seeded tracks are demo data and the screen says so.
    assert "Sample" in body


def test_a_track_added_through_the_interface_appears_in_the_catalog(client):
    response = client.post("/reach/catalog",
                           json={"title": "Harbour Lights", "artist_name": "Night Ferry"})
    assert response.get_json()["ok"] is True
    body = page(client, "/reach/catalog")
    assert "Harbour Lights" in body
    # The unentered stream count renders as the UNKNOWN badge — never 0,
    # never blank.
    assert "streams" in body and ">UNKNOWN</span>" in body


def test_the_interface_refuses_a_track_with_no_title(client):
    payload = client.post("/reach/catalog", json={"artist_name": "Night Ferry"}).get_json()
    assert payload["ok"] is False
    assert "title" in payload["error"]


def test_campaign_wizard_shows_unknown_profile_fields(client):
    recording = catalog.recording_by_slug("velvet-static")
    body = page(client, f"/reach/new?recording_id={recording['id']}")
    assert "Track intelligence profile" in body
    assert "UNKNOWN" in body
    assert "Rights attestation" in body
    assert "Release readiness" in body


# --- campaign screens -------------------------------------------------------

@pytest.mark.parametrize("suffix,marker", [
    ("", "Campaign health"),
    ("/discover", "Planned query families"),
    ("/opportunities", "Pipeline"),
    ("/review", "Queue"),
    ("/outreach", "Sender health"),
    ("/responses", "Record a response"),
    ("/placements", "Record a placement"),
])
def test_campaign_screens_render_with_content(client, live_campaign, suffix, marker):
    body = page(client, f"/reach/campaigns/{live_campaign}{suffix}")
    assert marker in body


def test_opportunity_detail_shows_evidence_and_score_reasons(client, live_campaign):
    target = campaigns.targets(live_campaign)[0]
    body = page(client, f"/reach/campaigns/{live_campaign}/targets/{target['id']}")
    assert "Why do we know this?" in body
    assert "Why this score?" in body
    assert "Outreach decision" in body
    assert "Read 20" in body  # every receipt shows its retrieval date


def test_review_screen_shows_the_exact_payload(client, live_campaign):
    body = page(client, f"/reach/campaigns/{live_campaign}/review")
    assert "Exactly what will be sent" in body
    assert "Approval verified against" in body
    assert "Source of every factual claim" in body
    assert "Independent verification of this address" in body


# --- global screens ---------------------------------------------------------

def test_every_completed_action_offers_the_next_step(client, live_campaign):
    """The guided flow: each screen hands the user its natural next action.

    Its first real user got stranded at every seam — a draft created with no
    pointer to where it went, an empty review queue with no way in.
    """
    from reach import campaigns

    target = campaigns.targets(live_campaign)[0]
    body = page(client, f"/reach/campaigns/{live_campaign}/targets/{target['id']}")
    assert f"/campaigns/{live_campaign}/review" in body, \
        "the target page must hand the user the review screen"
    assert "data-next=" in body or "Review the pitch" in body, \
        "drafting must navigate onward, or an existing draft must link to review"


def test_the_review_queue_is_never_a_dead_end(client, scouted_without_sender):
    """With no sender, nothing is READY — but qualified targets must still be
    draftable from the review screen itself, as its banner promises."""
    body = page(client, f"/reach/campaigns/{scouted_without_sender}/review")
    assert "Qualified — drafts start here" in body
    assert "Drafting works even while sending is disabled." in body


def test_campaign_creation_lands_on_discovery(client):
    from reach import catalog, profile

    recording = catalog.recording_by_slug("midnight-drive")
    profile_id = profile.get_or_create(recording["id"])
    for field, value in [("primary_genre", "synthwave"), ("microgenres", ["synthwave"]),
                         ("mood", ["driving"]), ("language", "English"),
                         ("comparable_artists", ["The Midnight"])]:
        profile.set_field(profile_id, field, value)
    catalog.attest_rights(recording["id"])
    payload = client.post("/reach/campaigns",
                          json={"recording_id": recording["id"]}).get_json()
    assert payload["ok"] is True
    assert payload["url"].endswith("/discover"),         "the next step after creating a campaign is running discovery"


def test_provider_health_never_shows_a_fake_green(client, live_campaign):
    body = page(client, "/reach/providers")
    assert "NOT CONNECTED" in body
    assert "MANUAL ONLY" in body
    assert "APPROVAL REQUIRED" in body
    # Spotify's restrictions are stated on the screen, not buried in code.
    assert "LLM blocked" in body
    assert "Contact resolution blocked" in body


def test_needs_you_lists_dsp_tasks_with_reasons(client, live_campaign):
    body = page(client, "/reach/needs-you")
    assert "Spotify for Artists editorial pitch" in body
    assert "Amazon Music for Artists new-release pitch" in body
    assert "Pandora AMP submission" in body
    assert "Why it isn&#39;t automated" in body or "Why it isn't automated" in body
    assert "Opening the destination does not mark this submitted." in body


def test_settings_shows_flags_suppression_and_audit(client, live_campaign):
    body = page(client, "/reach/settings")
    assert "reach.autopilot" in body
    assert "Emergency stops" in body
    assert "Audit trail" in body
    assert "Chain verified" in body


def test_contacts_screen_renders_and_old_url_still_works(client, live_campaign):
    body = page(client, "/reach/contacts")
    assert "Contacts" in body
    # The old bookmark keeps working.
    response = client.get("/reach/relationships")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/reach/contacts")


def test_fixture_mode_is_labelled_in_the_interface(client, live_campaign):
    body = page(client, "/reach")
    assert "Demo data — fixture corpus" in body


# --- API endpoints ----------------------------------------------------------

def test_evidence_endpoint_returns_provenance(client, live_campaign):
    target = campaigns.targets(live_campaign)[0]
    response = client.get(f"/reach/evidence/outlet/{target['outlet_id']}")
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["evidence"]
    assert payload["evidence"][0]["source_url"]


def test_provider_policy_endpoint_exposes_the_machine_readable_record(client):
    payload = client.get("/reach/providers/spotify/policy").get_json()
    assert payload["policy"]["maySendDataToLanguageModel"] is False
    assert payload["usage"]["maySendToLLM"] is False


def test_webhook_rejects_an_unsigned_request(client, live_campaign):
    response = client.post("/reach/webhooks/email", json={"type": "bounce",
                                                          "email": "x@example.test"})
    assert response.status_code == 401


def test_webhook_suppresses_on_a_signed_bounce(client, live_campaign):
    from reach import contacts

    response = client.post(
        "/reach/webhooks/email",
        json={"type": "bounce", "email": "bouncer@example.test"},
        headers={"X-Reach-Signature": "test-webhook-secret"},
    )
    assert response.get_json()["outcome"] == "SUPPRESSED"
    assert contacts.is_suppressed("bouncer@example.test") is not None


def test_campaign_creation_endpoint_rejects_an_incomplete_profile(client):
    recording = catalog.recording_by_slug("velvet-static")
    catalog.attest_rights(recording["id"])
    response = client.post("/reach/campaigns", json={"recording_id": recording["id"]})
    payload = response.get_json()
    assert payload["ok"] is False
    assert "incomplete" in payload["error"]


def test_metrics_on_the_overview_match_the_database(client, live_campaign):
    metrics = analytics.campaign_metrics(live_campaign)
    body = page(client, f"/reach/campaigns/{live_campaign}")
    assert str(metrics["discovered"]) in body
    assert str(metrics["placed"]) in body
