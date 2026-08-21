"""What the first real discovery run taught us, pinned as tests.

The pipeline's first contact with the live web qualified eight Cloudflare
challenge interstitials as curators named "Just a moment...", one 502 error
page named "502", and Spotify, YouTube, Wikipedia and Fiverr as outlets; a
magazine was typed PLAYLIST because its body said "playlist" once; and the run
itself died at the server's request timeout, leaving its in-flight job claimed
forever. Each of those is a test now.
"""

import pytest

from reach import campaigns, clock, entities, extractor, jobs, pipeline

from .conftest import drain


def fetch(campaign_id, url):
    jobs.enqueue("FETCH_PUBLIC_PAGE", {"url": url, "query": "test", "family": "PLAYLIST"},
                 campaign_id=campaign_id, idempotency_key=f"test:{url}")
    drain()


# --- interstitials are not the site behind them -----------------------------

def test_a_bot_challenge_page_is_detected():
    assert extractor.page_state({"title": "Just a moment...",
                                 "visible_text": "Checking your browser"}) \
        == extractor.PAGE_BOT_CHALLENGE


def test_an_error_status_is_an_error_page_regardless_of_body():
    assert extractor.page_state({"title": "Welcome", "visible_text": "fine"},
                                http_status=502) == extractor.PAGE_ERROR


def test_an_error_title_is_an_error_page():
    assert extractor.page_state({"title": "502", "visible_text": ""}) == extractor.PAGE_ERROR
    assert extractor.page_state({"title": "Access denied", "visible_text": ""}) \
        == extractor.PAGE_ERROR


def test_a_normal_page_is_ok():
    assert extractor.page_state({"title": "Voltage Journal",
                                 "visible_text": "Submit your music"}) == extractor.PAGE_OK


def test_a_challenge_page_never_becomes_a_target(campaign_id):
    """The Cloudflare interstitial is fetched, recorded as evidence of the
    attempt — and produces no outlet, no target, and no name."""
    fetch(campaign_id, "https://cfshield.example/submit")
    assert [t for t in campaigns.targets(campaign_id)
            if t["outlet_domain"] == "cfshield.example"] == []
    assert entities.outlet_by_domain("cfshield.example") is None


def test_an_error_page_never_becomes_a_target(campaign_id):
    fetch(campaign_id, "https://brokenupstream.example/playlists")
    assert [t for t in campaigns.targets(campaign_id)
            if t["outlet_domain"] == "brokenupstream.example"] == []


# --- platform domains are never outlets -------------------------------------

@pytest.mark.parametrize("domain", [
    "spotify.com", "open.spotify.com", "youtube.com", "wikipedia.org",
    "en.wikipedia.org", "fiverr.com", "github.com", "epidemicsound.com",
])
def test_platform_domains_are_recognised(domain):
    assert entities.is_platform_domain(domain) is True


@pytest.mark.parametrize("domain", [
    # bandcamp pages are run by labels and artists — the page owner IS an outlet
    "bandcamp.com", "aztecrecords.bandcamp.com",
    "newretrowave.com", "electrozombies.com", "darkwaveradio.net",
])
def test_real_outlets_are_not_platform_domains(domain):
    assert entities.is_platform_domain(domain) is False


def test_a_platform_page_never_becomes_a_target(campaign_id):
    """Belt at the resolve stage: even a platform page that arrives via a
    redirect produces no outlet."""
    extracted = {
        "page_state": extractor.PAGE_OK,
        "url": "https://open.spotify.com/genre/synthwave",
        "domain": "open.spotify.com",
        "classification": extractor.CURATOR,
        "outlet_name": "Spotify",
    }
    jobs.enqueue("RESOLVE_ENTITY", {"document_id": None, "extracted": extracted},
                 campaign_id=campaign_id, idempotency_key="test:platform-resolve")
    drain()
    assert [t for t in campaigns.targets(campaign_id)
            if "spotify" in (t["outlet_domain"] or "")] == []


# --- the title says what a site is ------------------------------------------

def test_the_title_outweighs_a_stray_keyword():
    """A magazine whose body mentions a playlist is a publication, not a
    playlist — Decibel Magazine came back typed PLAYLIST on the first run."""
    result = extractor.classify({
        "title": "Decibel Magazine",
        "meta_description": None,
        "visible_text": "Our editorial team publishes a playlist of the "
                        "month alongside reviews and premieres.",
    })
    assert result["classification"] == extractor.PUBLICATION


# --- interrupted runs recover on their own ----------------------------------

def test_a_stale_running_job_is_requeued():
    """A killed worker leaves its in-flight job claimed forever; nothing would
    ever claim it again."""
    from datetime import timedelta

    job_id = jobs.enqueue("FETCH_PUBLIC_PAGE", {"url": "https://bassforge.example/promo"},
                          idempotency_key="test:stale")
    from reach import db
    db.execute("UPDATE job_run SET status = ?, started_at = ? WHERE id = ?",
               (jobs.RUNNING, clock.to_iso(clock.now() - timedelta(minutes=10)), job_id))
    assert jobs.requeue_stale(older_than_seconds=300) == 1
    assert jobs.get(job_id)["status"] == jobs.PENDING


def test_a_fresh_running_job_is_left_alone():
    job_id = jobs.enqueue("FETCH_PUBLIC_PAGE", {"url": "https://bassforge.example/promo"},
                          idempotency_key="test:fresh")
    from reach import db
    db.execute("UPDATE job_run SET status = ?, started_at = ? WHERE id = ?",
               (jobs.RUNNING, clock.to_iso(clock.now()), job_id))
    assert jobs.requeue_stale(older_than_seconds=300) == 0


def test_a_time_budgeted_drain_stops_early_and_resumes(campaign_id):
    """The chunked web path: a zero-second budget processes nothing and loses
    nothing; the next drain finishes the run."""
    pipeline.start(campaign_id)
    assert pipeline.run_to_completion(campaign_id, max_seconds=0) == 0
    assert jobs.pending_count(campaign_id) > 0
    assert pipeline.run_to_completion(campaign_id) > 0
    assert jobs.pending_count(campaign_id) == 0


def test_the_start_endpoint_resumes_instead_of_replanning(campaign_id):
    """Tapping Run with work still queued must drain it, not plan a second run
    against a spent search budget."""
    from app import create_app

    client = create_app().test_client()
    pipeline.start(campaign_id)
    before = jobs.pending_count(campaign_id)
    assert before > 0
    response = client.post(f"/reach/campaigns/{campaign_id}/discover/start")
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["job_id"] is None, "queued work must resume, not replan"
    assert "pending" in payload
