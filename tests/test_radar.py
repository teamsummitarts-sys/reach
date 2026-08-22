"""Peer Radar: watchlist, sweep, coverage events, targeting.

Radar watches comparable artists and records which outlets cover them. These
tests hold it to the same honesty bar as discovery: every event is backed by a
page that actually mentions the artist, kinds are never guessed, budgets are
enforced and reported, and targeting an event goes through the real
qualification flow.
"""

from datetime import timedelta

import pytest

from app import create_app
from reach import campaigns, clock, compliance, db, entities, evidence, jobs, profile, radar, scoring
from reach.errors import ValidationError
from reach.fixtures import web as web_fixtures
from reach.providers import base as provider_base
from reach import fetcher

FIXTURE_ARTISTS = ["Grey Pulse", "Cold Veil"]


@pytest.fixture
def client():
    return create_app().test_client()


@pytest.fixture
def watched_fixture_artists():
    for name in FIXTURE_ARTISTS:
        radar.add_artist(name)
    return FIXTURE_ARTISTS


def swept():
    radar.start_sweep()
    radar.run_to_completion()
    return radar.events()


# --- watchlist --------------------------------------------------------------

def test_add_and_deactivate_watched_artist():
    artist_id = radar.add_artist("Grey Pulse")
    rows = radar.watched(active_only=True)
    assert [row["name"] for row in rows] == ["Grey Pulse"]
    assert rows[0]["source"] == radar.SOURCE_USER

    radar.deactivate_artist(artist_id)
    assert radar.watched(active_only=True) == []
    # The row survives deactivation: history is kept, not deleted.
    assert radar.watched()[0]["active"] == 0


def test_duplicates_are_refused_case_insensitively():
    radar.add_artist("Grey Pulse")
    with pytest.raises(ValidationError) as info:
        radar.add_artist("grey pulse")
    assert "already on the watchlist" in str(info.value)


def test_reactivating_a_deactivated_artist_reuses_the_row():
    artist_id = radar.add_artist("Grey Pulse")
    radar.deactivate_artist(artist_id)
    assert radar.add_artist("Grey Pulse") == artist_id
    assert radar.watched(active_only=True)[0]["id"] == artist_id


def test_the_watchlist_cap_is_enforced_with_a_clear_refusal():
    for index in range(radar.MAX_WATCHED):
        radar.add_artist(f"Artist {index}")
    with pytest.raises(ValidationError) as info:
        radar.add_artist("One Too Many")
    assert f"capped at {radar.MAX_WATCHED}" in str(info.value)

    # Deactivating one frees a slot.
    radar.deactivate_artist(radar.watched(active_only=True)[0]["id"])
    radar.add_artist("One Too Many")
    assert radar.active_count() == radar.MAX_WATCHED


def test_seeding_imports_profile_comparables_once(recording):
    profile_id = profile.get_or_create(recording["id"])
    profile.set_field(profile_id, "comparable_artists", FIXTURE_ARTISTS)

    added = radar.seed_from_profiles()
    assert added == FIXTURE_ARTISTS
    rows = radar.watched(active_only=True)
    assert {row["name"] for row in rows} == set(FIXTURE_ARTISTS)
    assert all(row["source"] == radar.SOURCE_PROFILE for row in rows)

    # Seeding never refills a list the user has emptied on purpose.
    for row in rows:
        radar.deactivate_artist(row["id"])
    assert radar.seed_from_profiles() == []
    assert radar.watched(active_only=True) == []


def test_seeding_respects_the_cap(recording):
    profile_id = profile.get_or_create(recording["id"])
    profile.set_field(profile_id, "comparable_artists",
                      [f"Artist {index}" for index in range(radar.MAX_WATCHED + 5)])
    added = radar.seed_from_profiles()
    assert len(added) == radar.MAX_WATCHED
    assert radar.active_count() == radar.MAX_WATCHED


# --- sweep ------------------------------------------------------------------

def test_sweep_produces_receipted_events_with_honest_kinds(watched_fixture_artists):
    events = swept()
    assert events, "the fixture corpus contains coverage of both artists"
    by_artist = {event["artist_name"]: event for event in events}

    premiere = by_artist["Grey Pulse"]
    assert premiere["kind"] == radar.PREMIERE
    assert premiere["domain"] == "echoline.example"
    assert "Grey Pulse" in premiere["excerpt"]

    # The Cold Veil page is coverage, but nothing on it says what kind — the
    # honest classification is UNKNOWN, never a guess.
    ambiguous = by_artist["Cold Veil"]
    assert ambiguous["kind"] == radar.KIND_UNKNOWN
    assert "Cold Veil" in ambiguous["excerpt"]

    for event in events:
        packet = evidence.get(event["evidence_id"])
        assert packet is not None
        assert packet["source_url"] == event["url"]
        assert packet["excerpt"]

    state = radar.state()
    assert state["last_sweep_finished_at"] is not None
    assert 0 < state["searches_used"] <= radar.SWEEP_SEARCH_CAP


def test_a_reswept_url_updates_the_event_instead_of_duplicating_it(watched_fixture_artists):
    first = swept()
    later = clock.now() + timedelta(hours=1)
    clock.freeze(later)
    second = swept()
    assert {event["id"] for event in first} == {event["id"] for event in second}
    assert all(event["retrieved_at"] == clock.now_iso() for event in second)


def test_the_search_cap_is_enforced_and_reported(monkeypatch, watched_fixture_artists):
    monkeypatch.setattr(radar, "SWEEP_SEARCH_CAP", 2)
    radar.start_sweep()
    radar.run_to_completion()
    assert radar.state()["searches_used"] == 2

    skipped = db.query(
        "SELECT result_json FROM job_run WHERE kind = 'RADAR_SEARCH' "
        "AND result_json LIKE '%SEARCH_CAP%'"
    )
    assert skipped, "capped searches are recorded as skips, not hidden"


def test_platform_domains_never_become_coverage_sources(monkeypatch):
    radar.add_artist("Grey Pulse")

    def fake_search(query, limit=10, campaign_id=None):
        return provider_base.AdapterResponse("open_web_search", provider_base.FIXTURE, [
            {"rank": 1, "url": "https://www.tiktok.com/@greypulse"},
            {"rank": 2, "url": "https://echoline.example/premiere-grey-pulse"},
        ])

    monkeypatch.setattr(radar.search_provider, "search", fake_search)
    swept()
    assert not db.query("SELECT id FROM coverage_event WHERE domain LIKE '%tiktok%'")
    fetched = db.query("SELECT payload_json FROM job_run WHERE kind = 'RADAR_FETCH'")
    assert fetched and all("tiktok" not in row["payload_json"] for row in fetched)
    assert {event["domain"] for event in radar.events()} == {"echoline.example"}


def test_an_interstitial_is_never_coverage_even_when_it_mentions_the_artist(monkeypatch):
    radar.add_artist("Grey Pulse")
    pages = dict(web_fixtures.PAGES)
    pages["https://cfshield.example/submit"] = web_fixtures._page(web_fixtures._html(
        "Just a moment...",
        "<p>Checking your browser. Grey Pulse. Enable JavaScript and cookies to continue.</p>",
    ))
    fetcher.set_transport(fetcher.FixtureTransport(pages=pages))
    fetcher.clear_robots_cache()

    def fake_search(query, limit=10, campaign_id=None):
        return provider_base.AdapterResponse("open_web_search", provider_base.FIXTURE, [
            {"rank": 1, "url": "https://cfshield.example/submit"},
        ])

    monkeypatch.setattr(radar.search_provider, "search", fake_search)
    assert swept() == []


def test_a_page_that_never_mentions_the_artist_is_skipped(monkeypatch):
    radar.add_artist("Grey Pulse")

    def fake_search(query, limit=10, campaign_id=None):
        return provider_base.AdapterResponse("open_web_search", provider_base.FIXTURE, [
            {"rank": 1, "url": "https://voltagejournal.example/contact"},
        ])

    monkeypatch.setattr(radar.search_provider, "search", fake_search)
    assert swept() == []
    skips = db.query(
        "SELECT result_json FROM job_run WHERE kind = 'RADAR_FETCH' "
        "AND result_json LIKE '%NO_ARTIST_MENTION%'"
    )
    assert skips, "irrelevant pages are recorded as skips — relevance is never fabricated"


# --- targeting --------------------------------------------------------------

def test_targeting_goes_through_real_qualification(campaign_id, watched_fixture_artists):
    events = swept()
    event = next(e for e in events if e["artist_name"] == "Grey Pulse")

    result = radar.target_event(event["id"], campaign_id)
    target = campaigns.get_target(result["target_id"])
    assert target is not None and target["campaign_id"] == campaign_id

    # The normal flow ran: score, risk and compliance all exist.
    assert scoring.latest_score(target["id"]) is not None
    assert scoring.latest_risk(target["id"]) is not None
    assert compliance.latest_decision(target["id"]) is not None

    # The coverage itself is attached to the outlet's evidence.
    packets = evidence.for_entity("outlet", result["outlet_id"], field="peer_coverage")
    assert packets and packets[0]["source_url"] == event["url"]

    refreshed = radar.get_event(event["id"])
    assert refreshed["targeted_target_id"] == target["id"]
    assert refreshed["outlet_id"] == result["outlet_id"]

    # Targeting twice returns the existing target instead of duplicating it.
    again = radar.target_event(event["id"], campaign_id)
    assert again["already_targeted"] is True
    assert again["target_id"] == target["id"]


def test_peer_coverage_component_is_unknown_without_coverage(campaign_id, watched_fixture_artists):
    campaign = campaigns.get(campaign_id)
    outlet_id = entities.ensure_outlet(
        name="Echo Line", url="https://echoline.example/premiere-grey-pulse",
        domain="echoline.example", kind="PUBLICATION",
    )
    outlet = entities.get_outlet(outlet_id)

    # No coverage events recorded: not measured, so UNKNOWN — never zero.
    before = scoring.score_opportunity(outlet, {}, campaign)
    assert before["components"]["peer_coverage"] is None

    swept()
    after = scoring.score_opportunity(outlet, {}, campaign)
    assert after["components"]["peer_coverage"] == 1.0


# --- the radar screen -------------------------------------------------------

def test_the_radar_page_renders_with_an_honest_never_swept_state(client):
    body = client.get("/reach/radar").get_data(as_text=True)
    assert client.get("/reach/radar").status_code == 200
    assert "Radar" in body
    assert "UNKNOWN" in body  # never swept is a stated gap, not a blank
    assert f"of {radar.SWEEP_SEARCH_CAP} per sweep" in body


def test_the_radar_page_seeds_and_renders_events(client, recording, watched_fixture_artists):
    swept()
    body = client.get("/reach/radar").get_data(as_text=True)
    assert "Grey Pulse" in body
    assert "echoline.example" in body
    assert "PREMIERE" in body


def test_auto_enqueue_happens_only_past_the_staleness_window(client):
    # First visit: never swept, so a sweep is enqueued.
    client.get("/reach/radar")
    assert radar.pending_jobs() > 0
    radar.run_to_completion()
    assert radar.pending_jobs() == 0

    # Fresh sweep on record: another visit enqueues nothing.
    client.get("/reach/radar")
    assert radar.pending_jobs() == 0

    # Past the window it becomes due again.
    clock.freeze(clock.now() + timedelta(days=radar.SWEEP_STALE_DAYS + 1))
    client.get("/reach/radar")
    assert radar.pending_jobs() > 0


def test_watch_and_deactivate_routes(client):
    response = client.post("/reach/radar/watch", json={"name": "Grey Pulse"})
    assert response.get_json()["ok"] is True
    artist_id = response.get_json()["artist_id"]

    refused = client.post("/reach/radar/watch", json={"name": "GREY PULSE"})
    assert refused.get_json()["ok"] is False
    assert "already on the watchlist" in refused.get_json()["error"]

    assert client.post(f"/reach/radar/watch/{artist_id}/deactivate").get_json()["ok"] is True
    assert radar.watched(active_only=True) == []


def test_sweep_route_drains_in_chunks_and_reports_honestly(client, watched_fixture_artists):
    data = client.post("/reach/radar/sweep").get_json()
    assert data["ok"] is True
    while data["pending"]:
        data = client.post("/reach/radar/sweep").get_json()
    assert data["state"]["last_sweep_finished_at"] is not None
    assert data["state"]["searches_used"] <= radar.SWEEP_SEARCH_CAP
    assert data["events"] == len(radar.events()) > 0


# --- Trend Watch (v2) --------------------------------------------------------

def test_trend_queries_come_only_from_supplied_profile_values(recording):
    # No profile genres, no watchlist: nothing to ask about, so no queries.
    assert radar.plan_trend_queries(radar.rbac.current_principal().tenant_id) == []

    profile_id = profile.get_or_create(recording["id"])
    profile.set_field(profile_id, "primary_genre", "dark electronic")
    profile.set_field(profile_id, "microgenres", ["industrial"])
    radar.add_artist("Grey Pulse")

    queries = radar.plan_trend_queries(radar.rbac.current_principal().tenant_id)
    texts = [q["query"] for q in queries]
    assert '"dark electronic" tiktok trend' in texts
    assert '"dark electronic" viral' in texts
    assert '"industrial" playlist trend' in texts
    assert '"Grey Pulse" viral OR tiktok' in texts
    assert len(queries) <= radar.TREND_SEARCH_CAP

    genre_tagged = [q for q in queries if q["genre_tag"]]
    assert all(q["artist_id"] is None for q in genre_tagged)


def test_trend_sweep_stores_genre_tagged_events_with_honest_kinds(complete_profile):
    radar.start_sweep()
    radar.run_to_completion()

    trends = radar.trend_events()
    assert trends, "the fixture corpus contains a trend article for the lane"
    assert all(event["kind"] in (radar.TREND, radar.KIND_UNKNOWN) for event in trends)

    pulsewire = next(e for e in trends if e["domain"] == "pulsewire.example")
    assert pulsewire["kind"] == radar.TREND
    assert pulsewire["genre_tag"] == "dark electronic"
    assert pulsewire["watched_artist_id"] is None
    assert "dark electronic" in pulsewire["excerpt"].lower()
    packet = evidence.get(pulsewire["evidence_id"])
    assert packet is not None and packet["source_url"] == pulsewire["url"]

    # A page found by a trend query with no clear trend signal stays UNKNOWN —
    # it is never promoted into a fabricated trend.
    assert not any(event["kind"] == radar.TREND and "trend" not in
                   ((event["title"] or "") + (event["excerpt"] or "")).lower()
                   and "viral" not in ((event["title"] or "") + (event["excerpt"] or "")).lower()
                   for event in trends if event["domain"] == "pulsewire.example")


def test_genre_level_events_have_null_artist_and_stay_out_of_the_coverage_feed(complete_profile):
    radar.start_sweep()
    radar.run_to_completion()

    coverage = radar.events()
    assert all(event["watched_artist_id"] for event in coverage)
    assert all(event["kind"] != radar.TREND for event in coverage)

    genre_level = [e for e in radar.trend_events() if e["watched_artist_id"] is None]
    assert genre_level, "genre queries produced genre-level events"
    for event in genre_level:
        assert event["genre_tag"]
        assert event["artist_name"] is None
        assert radar.get_event(event["id"]) is not None


def test_trend_family_cap_is_enforced_inside_the_total(monkeypatch, complete_profile, watched_fixture_artists):
    radar.start_sweep()
    jobs.run_pending(limit=1)  # plan the full query family first
    monkeypatch.setattr(radar, "TREND_SEARCH_CAP", 1)
    radar.run_to_completion()

    state = radar.state()
    assert state["trend_searches_used"] == 1
    assert state["searches_used"] <= radar.SWEEP_SEARCH_CAP
    # The per-artist coverage family still ran in full alongside the capped
    # trend family: 2 artists x 3 queries, plus the one trend search.
    assert state["searches_used"] == 7

    skipped = db.query(
        "SELECT result_json FROM job_run WHERE kind = 'RADAR_SEARCH' "
        "AND result_json LIKE '%TREND_CAP%'"
    )
    assert skipped, "capped trend searches are recorded as skips, not hidden"


def test_the_sweep_caps_are_the_specified_ones():
    assert radar.SWEEP_SEARCH_CAP == 80
    assert radar.TREND_SEARCH_CAP == 20


def test_platform_domains_never_become_trend_sources(monkeypatch, complete_profile):
    def fake_search(query, limit=10, campaign_id=None):
        return provider_base.AdapterResponse("open_web_search", provider_base.FIXTURE, [
            {"rank": 1, "url": "https://www.tiktok.com/tag/darkelectronic"},
            {"rank": 2, "url": "https://www.instagram.com/explore/tags/industrial/"},
            {"rank": 3, "url": "https://pulsewire.example/trends/dark-electronic-tiktok"},
        ])

    monkeypatch.setattr(radar.search_provider, "search", fake_search)
    radar.start_sweep()
    radar.run_to_completion()

    domains = {event["domain"] for event in radar.trend_events()}
    assert domains == {"pulsewire.example"}
    assert not db.query(
        "SELECT id FROM coverage_event WHERE domain LIKE '%tiktok%' OR domain LIKE '%instagram%'"
    )


def test_a_trend_event_can_be_targeted_through_the_same_flow(campaign_id):
    radar.start_sweep()
    radar.run_to_completion()
    event = next(e for e in radar.trend_events() if e["domain"] == "pulsewire.example")

    result = radar.target_event(event["id"], campaign_id)
    target = campaigns.get_target(result["target_id"])
    assert target is not None
    assert scoring.latest_score(target["id"]) is not None
    assert scoring.latest_risk(target["id"]) is not None
    assert compliance.latest_decision(target["id"]) is not None

    # A genre-level finding attaches trend evidence, not peer coverage — the
    # outlet wrote about the lane, not about a watched artist.
    packets = evidence.for_entity("outlet", result["outlet_id"], field="trend_coverage")
    assert packets and packets[0]["source_url"] == event["url"]
    assert not evidence.for_entity("outlet", result["outlet_id"], field="peer_coverage")

    refreshed = radar.get_event(event["id"])
    assert refreshed["targeted_target_id"] == target["id"]


def test_the_radar_page_renders_both_sections_with_both_budgets(client, complete_profile, watched_fixture_artists):
    radar.start_sweep()
    radar.run_to_completion()
    body = client.get("/reach/radar").get_data(as_text=True)
    assert "Coverage found" in body
    assert "Trends in your lane" in body
    assert f"of {radar.SWEEP_SEARCH_CAP} per sweep" in body
    assert "trend family" in body and f"of {radar.TREND_SEARCH_CAP}" in body
    assert "pulsewire.example" in body
    assert "TREND" in body


def test_jobs_are_cancellable_mid_sweep(watched_fixture_artists):
    radar.start_sweep()
    jobs.run_pending(limit=1)  # plan only
    pending = db.query("SELECT id FROM job_run WHERE kind LIKE 'RADAR%' AND status = 'PENDING'")
    assert pending
    for row in pending:
        jobs.cancel(row["id"])
    radar.run_to_completion()
    assert radar.events() == []
