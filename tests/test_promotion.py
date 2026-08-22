"""Paid promotion: screening, campaign fit, spend planning.

The honesty bar this module has to clear is narrower than discovery's and
sharper: REACH is telling an artist which services are safe to pay. So every
verdict here is backed by the service's own words, blocking is never inferred
from a third party's opinion, a denial is never read as an offer, UNKNOWN never
softens into "probably fine", and no arrangement REACH might have with a service
can reach the decision — the screening function is handed a projection with the
commercial fields structurally absent.
"""

import copy
import json
from datetime import timedelta

import pytest

from app import create_app
from reach import (campaigns, clock, db, entities, evidence, extractor, fetcher,
                   humanactions, jobs, promotion, rbac, sanitizer)
from reach.errors import PermissionDenied, ValidationError
from reach.fixtures import web as web_fixtures
from reach.providers import base as provider_base

SERVICE_URLS = [
    "https://levelpath.example/",
    "https://levelpath.example/pricing",
    "https://levelpath.example/terms",
    "https://presswire.example/",
    "https://presswire.example/terms",
    "https://streamboost.example/",
    "https://playlistgold.example/",
    "https://gatedpromo.example/",
    "https://gatedpromo.example/pricing",
    "https://honestpromo.example/",
    "https://promoreview.example/best-promo-services",
]


@pytest.fixture
def client():
    return create_app().test_client()


def use_search(monkeypatch, urls):
    """Point the sweep at an exact result set, so a test pins behaviour rather
    than fixture-corpus ranking."""
    def fake_search(query, limit=10, campaign_id=None):
        return provider_base.AdapterResponse(
            "open_web_search", provider_base.FIXTURE,
            [{"rank": index, "url": url} for index, url in enumerate(urls, start=1)],
            note="fixture",
        )
    monkeypatch.setattr(promotion.search_provider, "search", fake_search)


def swept(monkeypatch, urls=None):
    use_search(monkeypatch, urls if urls is not None else SERVICE_URLS)
    promotion.start_sweep()
    promotion.run_to_completion()
    return promotion.services()


def by_domain(domain):
    return promotion.service_by_domain(domain)


def page(client, path):
    response = client.get(path)
    assert response.status_code == 200, f"{path} returned {response.status_code}"
    return response.get_data(as_text=True)


def sanitize_page(url):
    result = fetcher.fetch(url)
    return sanitizer.sanitize(result.text(), base_url=result.final_url)


def page_haystack(url):
    """The page text exactly as the rule tables read it: title, description and
    visible text joined, whitespace collapsed. An excerpt has to be a literal
    substring of this or it is not a receipt."""
    sanitized = sanitize_page(url)
    joined = " ".join(filter(None, [sanitized.get("title") or "",
                                    sanitized.get("meta_description") or "",
                                    sanitized.get("visible_text") or ""]))
    return " ".join(joined.split()).lower()


def with_pages(extra):
    """A per-test corpus copy, so mutating a page never leaks between tests."""
    pages = copy.deepcopy(web_fixtures.PAGES)
    pages.update(extra)
    fetcher.set_transport(fetcher.FixtureTransport(pages=pages))
    fetcher.clear_robots_cache()
    return pages


def offer_page(title, body, description=None):
    return web_fixtures._page(web_fixtures._html(title, body, description=description))


def reasons_of(service):
    return [item["text"] for item in promotion._latest_reasons(service["id"])]


# --- 1. hard blocks ---------------------------------------------------------

@pytest.mark.parametrize("phrase", [
    "Guaranteed 20,000 Spotify streams",
    "Guaranteed placement on Spotify playlists",
    "Trigger Discover Weekly in 48 hours",
    "Buy 5,000 Spotify saves",
])
def test_guarantee_language_in_an_offer_is_a_hard_block(phrase):
    sanitized = {"title": "Promo", "meta_description": "",
                 "visible_text": f"Our packages start at $99. {phrase}."}
    signals = promotion.hard_block_signals(sanitized)
    assert signals, f"{phrase!r} should produce a hard-block signal"
    assert all(signal["excerpt"] for signal in signals)


def test_guaranteed_streams_services_are_blocked_end_to_end(monkeypatch):
    swept(monkeypatch)
    streamboost = by_domain("streamboost.example")
    playlistgold = by_domain("playlistgold.example")
    assert streamboost["screening_status"] == promotion.BLOCKED
    assert playlistgold["screening_status"] == promotion.BLOCKED
    assert "Guaranteed streams" in streamboost["block_reason"]
    assert "Guaranteed playlist placement" in playlistgold["block_reason"]

    # Blocking is evidence, not opinion: the excerpt is on the page.
    packets = [row for row in promotion.current_packets(streamboost["id"])
               if row["supports_field"] == "stream_guarantees"]
    assert packets
    haystack = page_haystack("https://streamboost.example/")
    for row in packets:
        stripped = row["excerpt"].strip("…").strip().lower()
        assert stripped in haystack, f"excerpt is not on the page: {stripped[:60]!r}"


# --- 2. legitimate paid consideration ---------------------------------------

def test_a_guaranteed_listen_with_curator_discretion_is_screened(monkeypatch):
    swept(monkeypatch)
    service = by_domain("levelpath.example")
    assert service["screening_status"] == promotion.SCREENED
    assert service["category"] == promotion.INDIVIDUAL_CURATOR
    # The distinction the whole module exists to hold: a guaranteed listen is
    # not a guaranteed placement.
    assert service["guaranteed_feedback"] == promotion.TRUE
    assert service["placement_discretionary"] == promotion.TRUE
    assert service["guaranteed_streams"] == promotion.TRISTATE_UNKNOWN
    assert service["pricing_min"] == 14.0
    assert evidence.for_entity("promotion_service", service["id"])


# --- 3. paid media and advertising ------------------------------------------

def test_sold_coverage_is_caution_not_blocked(monkeypatch):
    swept(monkeypatch)
    service = by_domain("presswire.example")
    assert service["screening_status"] == promotion.CAUTION
    assert service["category"] == promotion.PAID_MEDIA_OUTLET
    assert service["guaranteed_coverage"] == promotion.TRUE
    assert service["guaranteed_playlist_placement"] != promotion.TRUE
    assert any("disclosure" in reason for reason in reasons_of(service))


def test_guarantee_language_outside_an_offer_is_not_a_block():
    """The second half of the guard: unnegated guarantee language still needs
    the site's own offer context — a price, a package, a buy or submit flow."""
    without_offer = {"title": "Our mission", "meta_description": "",
                     "visible_text": "Artists deserve better. Guaranteed 20,000 Spotify "
                                     "streams is what the industry has come to expect."}
    assert promotion.hard_block_signals(without_offer) == []

    with_offer = dict(without_offer,
                      visible_text=without_offer["visible_text"] + " Our packages start at $99.")
    assert promotion.hard_block_signals(with_offer), "the same words in an offer do block"


def test_guaranteed_impressions_are_an_advertising_product_not_a_block():
    sanitized = {"title": "Ad packages", "meta_description": "",
                 "visible_text": "Our ad packages start at $500. Guaranteed 100,000 "
                                 "impressions across the network."}
    assert promotion.hard_block_signals(sanitized) == []


# --- 4. UNKNOWN never means safe --------------------------------------------

def test_login_walled_pricing_stays_unknown_with_the_reason(monkeypatch):
    swept(monkeypatch)
    service = by_domain("gatedpromo.example")
    assert service["screening_status"] == promotion.UNKNOWN
    assert any("login" in reason for reason in reasons_of(service))
    assert service["pricing_min"] is None


def test_unknown_pricing_renders_as_unknown_never_zero(client, monkeypatch, campaign_id):
    swept(monkeypatch)
    promotion.set_campaign_promotion(campaign_id, True)
    body = page(client, f"/reach/campaigns/{campaign_id}/promotion?view=unknown")
    assert "gatedpromo.example" in body
    assert ">UNKNOWN</span>" in body
    assert "$0" not in body


def test_a_stale_price_is_shown_with_its_date_not_as_current(monkeypatch, campaign_id):
    swept(monkeypatch)
    service = by_domain("levelpath.example")
    assert promotion.pricing_is_current(service) is True
    clock.freeze(clock.now() + timedelta(days=promotion.RESCREEN_DAYS_SCREENED + 5))
    assert promotion.pricing_is_current(promotion.get_service(service["id"])) is False


# --- 5. staleness and policy change -----------------------------------------

def test_a_screening_past_its_window_reads_as_out_of_date(client, monkeypatch, campaign_id):
    swept(monkeypatch)
    promotion.set_campaign_promotion(campaign_id, True)
    service = by_domain("levelpath.example")
    assert promotion.is_stale(service) is False

    clock.freeze(clock.now() + timedelta(days=promotion.RESCREEN_DAYS_SCREENED + 1))
    assert promotion.is_stale(promotion.get_service(service["id"])) is True
    body = page(client, f"/reach/campaigns/{campaign_id}/promotion")
    assert "SCREENING OUT OF DATE" in body
    # Due again, so the page enqueues the rescreen itself — audited, and only
    # for a principal who may run discovery.
    assert promotion.pending_jobs() > 0
    assert any(row["action"] == "promotion.sweep_autostarted"
               for row in db.query("SELECT action FROM audit_event"))


def test_a_changed_policy_page_flags_a_rescreen(client, monkeypatch, campaign_id):
    with_pages({})
    swept(monkeypatch)
    promotion.set_campaign_promotion(campaign_id, True)
    service = by_domain("levelpath.example")
    assert service["manual_review_required"] == 0

    with_pages({"https://levelpath.example/terms": offer_page(
        "Terms | Level Path",
        "<p>Level Path Reviews Ltd, Bristol. The curator decides what gets covered. "
        "Refund policy: refunded in full. We rewrote these terms this week.</p>")})
    promotion.request_rescan(service["id"])
    promotion.run_to_completion()

    refreshed = promotion.get_service(service["id"])
    assert refreshed["manual_review_required"] == 1
    assert any(row["action"] == "promotion.policy_changed"
               for row in db.query("SELECT action FROM audit_event"))
    body = page(client, f"/reach/campaigns/{campaign_id}/promotion")
    assert "NEEDS RESCREENING" in body


# --- 6. conflict of interest -------------------------------------------------

def test_screening_cannot_see_a_commercial_relationship(monkeypatch):
    swept(monkeypatch)
    service = by_domain("streamboost.example")
    db.update("promotion_service", service["id"],
              {"commercial_relationship_type": "AFFILIATE",
               "commercial_disclosure": "REACH earns a commission"})

    facts = promotion.screening_inputs(promotion.get_service(service["id"]))
    assert "commercial_relationship_type" not in facts
    assert "commercial_disclosure" not in facts

    packets = promotion.current_packets(service["id"])
    with_affiliate = promotion.screen_service(facts, packets)
    db.update("promotion_service", service["id"],
              {"commercial_relationship_type": "NONE", "commercial_disclosure": None})
    without = promotion.screen_service(
        promotion.screening_inputs(promotion.get_service(service["id"])), packets)

    assert with_affiliate["status"] == promotion.BLOCKED
    assert with_affiliate["status"] == without["status"]
    assert with_affiliate["reasons"] == without["reasons"]


def test_the_persisted_verdict_is_identical_with_and_without_an_affiliate_deal(monkeypatch):
    """The end-to-end half of commercial blindness.

    Injecting a leak just before the db.update in run_screening — "if AFFILIATE
    and BLOCKED, soften to CAUTION" — passed the projection tests. This asserts
    the row that actually lands.
    """
    swept(monkeypatch)
    service = by_domain("streamboost.example")

    promotion.set_commercial_relationship(service["id"], "NONE")
    promotion.run_screening(service["id"])
    clean = promotion.get_service(service["id"])
    clean_row = promotion.latest_screening(service["id"])

    promotion.set_commercial_relationship(service["id"], "AFFILIATE",
                                          "REACH earns a commission")
    promotion.run_screening(service["id"])
    affiliated = promotion.get_service(service["id"])
    affiliated_row = promotion.latest_screening(service["id"])

    assert affiliated["screening_status"] == promotion.BLOCKED
    assert affiliated["screening_status"] == clean["screening_status"]
    assert affiliated["block_reason"] == clean["block_reason"]
    for field in ("status", "score", "components_json", "signals_json", "reasons_json",
                  "block_reason"):
        assert affiliated_row[field] == clean_row[field], f"{field} moved with the deal"


def test_the_screening_projection_exposes_no_commercial_field(monkeypatch):
    swept(monkeypatch)
    service = by_domain("levelpath.example")
    promotion.set_commercial_relationship(service["id"], "PARTNER", "Joint venture")
    facts = promotion.screening_inputs(promotion.get_service(service["id"]))

    # The whole key set, not two literal names: a new commercial column added
    # to the projection later has to fail this.
    expected = {
        "id", "name", "canonical_domain", "company_name", "category", "service_types",
        "pricing_model", "pricing_min", "pricing_max", "pricing_currency",
        "pricing_last_verified_at", "business_model_summary", "terms_url", "contact_url",
        "submission_url", "pricing_url", "page_ok_count", "manual_review_required",
        "fact_freshness", "window_days", "pricing_login_walled",
    } | set(promotion.TRISTATE_FIELDS)
    assert set(facts) == expected
    assert not any("commercial" in key for key in facts)
    assert "PARTNER" not in json.dumps(facts)
    assert "Joint venture" not in json.dumps(facts)


def test_a_commercial_relationship_is_disclosed_on_card_and_dossier(client, monkeypatch,
                                                                    campaign_id):
    swept(monkeypatch)
    promotion.set_campaign_promotion(campaign_id, True)
    service = by_domain("levelpath.example")
    db.update("promotion_service", service["id"],
              {"commercial_relationship_type": "AFFILIATE",
               "commercial_disclosure": "REACH earns a commission on referrals"})

    card = page(client, f"/reach/campaigns/{campaign_id}/promotion")
    assert "Commercial relationship: AFFILIATE" in card
    dossier = page(client, f"/reach/promotion/services/{service['id']}?campaign_id={campaign_id}")
    assert "Commercial relationship: AFFILIATE" in dossier
    assert "REACH earns a commission on referrals" in dossier


# --- 7. caps ------------------------------------------------------------------

def test_the_search_cap_is_a_recorded_stop_rule(monkeypatch):
    use_search(monkeypatch, SERVICE_URLS)
    promotion.start_sweep()
    jobs.run_pending(limit=1)  # plan the full query family first
    monkeypatch.setattr(promotion, "SWEEP_SEARCH_CAP", 2)
    promotion.run_to_completion()

    assert promotion.state()["searches_used"] == 2
    skipped = db.query(
        "SELECT result_json FROM job_run WHERE kind = 'PROMO_SEARCH' "
        "AND result_json LIKE '%SEARCH_CAP%'")
    assert skipped, "a spent cap is recorded as a skip, never hidden"


def test_the_caps_are_the_specified_ones_and_are_actually_applied(monkeypatch):
    assert promotion.SWEEP_SEARCH_CAP == 40
    assert promotion.RESULTS_PER_SEARCH == 8
    assert promotion.RESCREEN_DAYS_SCREENED == 30
    assert promotion.RESCREEN_DAYS_CAUTION == 14
    assert promotion.RESCREEN_DAYS_BLOCKED == 7
    assert promotion.RESCREEN_DAYS_UNKNOWN == 14
    assert promotion.PROMO_SWEEP_STALE_DAYS == 14

    # The constants have to reach the provider call and the planner, not just
    # sit in the module.
    seen = []

    def spy(query, limit=10, campaign_id=None):
        seen.append(limit)
        return provider_base.AdapterResponse("open_web_search", provider_base.FIXTURE, [])

    monkeypatch.setattr(promotion.search_provider, "search", spy)
    promotion.start_sweep()
    promotion.run_to_completion()
    assert seen, "the sweep ran at least one search"
    assert set(seen) == {promotion.RESULTS_PER_SEARCH}
    assert len(seen) <= promotion.SWEEP_SEARCH_CAP
    assert len(promotion.plan_queries()) <= promotion.SWEEP_SEARCH_CAP


def test_a_spent_cap_is_reported_rather_than_read_as_completion(client, monkeypatch,
                                                                campaign_id):
    use_search(monkeypatch, SERVICE_URLS)
    promotion.set_campaign_promotion(campaign_id, True)
    promotion.start_sweep()
    jobs.run_pending(limit=1)
    monkeypatch.setattr(promotion, "SWEEP_SEARCH_CAP", 1)
    promotion.run_to_completion()

    not_run = promotion.skipped_queries()
    assert not_run > 0
    body = page(client, f"/reach/campaigns/{campaign_id}/promotion")
    assert "planned queries were not run" in body
    assert str(not_run) in body


# --- 8. fit is a separate axis from safety -----------------------------------

def test_fit_and_screening_are_independent(monkeypatch, campaign_id):
    swept(monkeypatch)
    promotion.set_campaign_promotion(campaign_id, True)
    promotion.compute_fits(campaign_id)

    screened = by_domain("levelpath.example")
    assert screened["screening_status"] == promotion.SCREENED
    fit = promotion.latest_fit(screened["id"], campaign_id)
    assert fit is not None

    blocked = by_domain("streamboost.example")
    assert promotion.latest_fit(blocked["id"], campaign_id) is None
    items = promotion.campaign_services(campaign_id)
    blocked_item = next(item for item in items
                        if item["service"]["canonical_domain"] == "streamboost.example")
    assert blocked_item["fit_score"] is None


def test_fit_components_are_unknown_not_zero_without_profile_values(recording, attested):
    campaign_id = campaigns.create(recording["id"], territories=[])
    campaign = campaigns.get(campaign_id)
    service_id = promotion.ensure_service("Empty", "empty.example", "https://empty.example/")
    result = promotion.fit_for_campaign(promotion.get_service(service_id), campaign, {})
    assert result["components"]["genre_overlap"] is None
    assert result["components"]["budget_fit"] is None


# --- 9. entity resolution ----------------------------------------------------

def test_pages_of_one_domain_accumulate_on_one_service(monkeypatch):
    swept(monkeypatch)
    rows = db.query("SELECT id FROM promotion_service WHERE canonical_domain = ?",
                    ("levelpath.example",))
    assert len(rows) == 1
    service = by_domain("levelpath.example")
    urls = {row["source_url"] for row in evidence.for_entity("promotion_service", service["id"])}
    assert len(urls) >= 2

    before = len(promotion.services())
    promotion.start_sweep()
    promotion.run_to_completion()
    assert len(promotion.services()) == before


def test_a_merge_hides_the_loser_and_reparents_its_evidence(monkeypatch):
    swept(monkeypatch)
    winner = by_domain("levelpath.example")
    loser_id = promotion.ensure_service("Level Path", "levelpath2.example",
                                        "https://levelpath2.example/")
    evidence.record(entity_type="promotion_service", entity_id=loser_id,
                    supports_field="business_model", value={"category": "UNKNOWN"},
                    source_url="https://levelpath2.example/", source_domain="levelpath2.example",
                    source_type="OFFICIAL", excerpt="x", confidence=0.5)

    entities.merge("promotion_service", winner["id"], loser_id)
    domains = {row["canonical_domain"] for row in promotion.services()}
    assert "levelpath2.example" not in domains
    assert any(row["source_domain"] == "levelpath2.example"
               for row in evidence.for_entity("promotion_service", winner["id"]))
    # A post-merge sweep accumulates on the winner rather than reviving the loser.
    assert promotion.service_by_domain("levelpath2.example")["id"] == winner["id"]
    assert promotion.ensure_service("Level Path", "levelpath2.example",
                                    "https://levelpath2.example/") == winner["id"]


# --- 10. the plan ------------------------------------------------------------

@pytest.fixture
def planned(monkeypatch, campaign_id):
    swept(monkeypatch)
    promotion.set_campaign_promotion(campaign_id, True, budget_amount=1000,
                                     budget_currency="USD")
    return campaign_id


def test_payment_is_tracked_separately_from_the_review_outcome(planned):
    service = by_domain("levelpath.example")
    paid = promotion.add_plan_item(planned, service_id=service["id"],
                                   category="CURATOR_SUBMISSIONS", amount=100)
    unpaid = promotion.add_plan_item(planned, label="Meta Ads",
                                     category="DIGITAL_ADVERTISING", amount=50)
    unknown = promotion.add_plan_item(planned, label="Manager retainer", category="OTHER")

    assert promotion.plan_totals(planned)["planned"] == 150
    assert promotion.plan_totals(planned)["spent"] == 0

    promotion.set_plan_status(paid, promotion.PAID)
    totals = promotion.plan_totals(planned)
    assert totals["spent"] == 100 and totals["planned"] == 50

    # A declined pitch usually keeps the fee: paid stays spent.
    promotion.set_plan_status(paid, promotion.DECLINED)
    assert promotion.plan_totals(planned)["spent"] == 100
    promotion.set_plan_status(paid, promotion.IN_REVIEW)
    assert promotion.plan_totals(planned)["spent"] == 100

    # A refund leaves both sums.
    promotion.set_plan_status(paid, promotion.REFUNDED)
    totals = promotion.plan_totals(planned)
    assert totals["spent"] == 0 and totals["planned"] == 50

    # An unpaid decline leaves Planned.
    promotion.set_plan_status(unpaid, promotion.DECLINED)
    assert promotion.plan_totals(planned)["planned"] == 0

    totals = promotion.plan_totals(planned)
    assert totals["unknown_amounts"] == 1
    assert totals["remaining"] == 1000
    assert unknown in {row["id"] for row in promotion.plan_items(planned)}


def test_remaining_is_unknown_when_no_budget_was_entered(monkeypatch, campaign_id):
    swept(monkeypatch)
    promotion.set_campaign_promotion(campaign_id, True)
    promotion.add_plan_item(campaign_id, label="Meta Ads", category="OTHER", amount=25)
    totals = promotion.plan_totals(campaign_id)
    assert totals["budget"] is None
    assert totals["remaining"] is None


def test_a_blocked_service_is_refused_from_the_plan(client, planned):
    blocked = by_domain("streamboost.example")
    with pytest.raises(ValidationError) as info:
        promotion.add_plan_item(planned, service_id=blocked["id"], category="OTHER", amount=10)
    assert "BLOCKED" in str(info.value)

    response = client.post(f"/reach/promotion/services/{blocked['id']}/plan",
                           json={"campaign_id": planned, "category": "OTHER"})
    assert response.get_json()["ok"] is False

    body = page(client, f"/reach/campaigns/{planned}/promotion?view=blocked")
    assert promotion.BLOCKED_STATEMENT in body
    # The blocked view renders blocked services and nothing else, so no
    # add-to-plan control may appear anywhere on it.
    assert "streamboost.example" in body
    assert "Add to plan" not in body
    assert "Add to Promotion Plan" not in body
    assert promotion.BLOCKED_SUPPORT in body

    dossier = page(client, f"/reach/promotion/services/{blocked['id']}?campaign_id={planned}")
    assert "Add to Promotion Plan" not in dossier
    assert "Prepare submission task" not in dossier


def test_planning_needs_the_permission(planned):
    service = by_domain("levelpath.example")
    with rbac.as_role(rbac.VIEW_ONLY):
        with pytest.raises(PermissionDenied):
            promotion.add_plan_item(planned, service_id=service["id"], category="OTHER")


# --- 11. opt-in ---------------------------------------------------------------

def test_paid_promotion_is_off_by_default_but_always_reachable(client, campaign_id):
    assert promotion.enabled_for(campaign_id) is False
    overview = page(client, f"/reach/campaigns/{campaign_id}")
    assert f"/reach/campaigns/{campaign_id}/promotion" in overview

    body = page(client, f"/reach/campaigns/{campaign_id}/promotion")
    assert "Include paid promotion opportunities" in body
    assert promotion.CATEGORY_PROMISE in body
    # No sweep runs for a campaign that never opted in.
    assert promotion.pending_jobs() == 0

    client.post(f"/reach/campaigns/{campaign_id}/promotion/enable",
                json={"enabled": "1", "budget_amount": "500", "budget_currency": "USD"})
    assert promotion.enabled_for(campaign_id) is True
    assert campaigns.get(campaign_id)["promotion_budget_amount"] == 500.0


def test_campaign_creation_carries_the_opt_in(recording, attested):
    default_campaign = campaigns.create(recording["id"])
    assert campaigns.get(default_campaign)["paid_promotion_enabled"] == 0

    opted = campaigns.create(recording["id"], name="With promo",
                             paid_promotion_enabled=True, promotion_budget_amount=250.0,
                             promotion_budget_currency="EUR")
    row = campaigns.get(opted)
    assert row["paid_promotion_enabled"] == 1
    assert row["promotion_budget_amount"] == 250.0


def test_dashboard_actions_appear_only_with_real_counts(client, monkeypatch, campaign_id):
    body = page(client, "/reach/")
    assert "screened paid promotion" not in body

    swept(monkeypatch)
    promotion.set_campaign_promotion(campaign_id, True)
    promotion.compute_fits(campaign_id)
    counts = promotion.dashboard_counts()
    assert counts["screened"] >= 1 and counts["matched"] >= 1

    body = page(client, "/reach/")
    assert "screened paid promotion option" in body
    assert "match this release" in body


# --- 12. Needs You handoff ----------------------------------------------------

def test_the_handoff_is_copy_ready_and_never_an_outreach_submission(monkeypatch, campaign_id):
    swept(monkeypatch)
    service = by_domain("levelpath.example")
    task_id = promotion.create_handoff_task(service["id"], campaign_id)
    task = humanactions.get(task_id)

    assert task["reason"] == humanactions.REASON_PAID
    assert task["target_id"] is None
    assert task["title"] == f"Submit to {service['name']}"
    values = {field["label"]: field["value"] for field in task["fields"]}
    assert values["Track name"] == "Midnight Drive"
    assert "Campaign notes" in values
    # Each field REACH cannot know says so by name — not "some value somewhere
    # is UNKNOWN", which a fabricated artwork line would still satisfy.
    assert values["Artwork"] == "UNKNOWN — attach before submitting"
    assert values["Private streaming link"] == "UNKNOWN — add before submitting"
    assert values["Comparable artists"]
    assert not any(str(value).strip() == "" for value in values.values())

    # Marking it submitted records no submission and moves no target: a
    # promotion purchase is not outreach.
    humanactions.set_status(task_id, humanactions.SUBMITTED)
    assert db.query("SELECT id FROM submission") == []
    assert campaigns.get(campaign_id)["status"] != campaigns.COMPLETED
    assert all(row["status"] != campaigns.SUBMITTED
               for row in campaigns.targets(campaign_id))


def test_an_unreadable_price_is_never_presented_as_free_on_a_handoff(monkeypatch,
                                                                     campaign_id):
    swept(monkeypatch)
    service = by_domain("levelpath.example")
    db.update("promotion_service", service["id"], {"pricing_min": None, "pricing_max": None})
    task = humanactions.get(promotion.create_handoff_task(service["id"], campaign_id))
    values = {field["label"]: field["value"] for field in task["fields"]}
    assert values["Cost"] == "UNKNOWN — REACH could not read this service's price"


def test_a_handoff_is_refused_for_a_service_that_is_not_screened(monkeypatch, campaign_id):
    swept(monkeypatch)
    for domain in ("streamboost.example", "presswire.example", "gatedpromo.example"):
        service = by_domain(domain)
        with pytest.raises(ValidationError):
            promotion.create_handoff_task(service["id"], campaign_id)


# --- 13. overrides ------------------------------------------------------------

def test_an_override_needs_a_reason_and_leaves_the_evidence_alone(client, monkeypatch,
                                                                  campaign_id):
    from reach import audit

    swept(monkeypatch)
    service = by_domain("levelpath.example")
    before = len(evidence.for_entity("promotion_service", service["id"]))
    screened_at = service["last_screened_at"]

    with pytest.raises(ValidationError):
        promotion.override_screening(service["id"], promotion.CAUTION, "")

    promotion.override_screening(service["id"], promotion.CAUTION, "Reviewing their terms")
    refreshed = promotion.get_service(service["id"])
    assert refreshed["screening_status"] == promotion.CAUTION
    assert refreshed["last_screened_at"] == screened_at, "an override screens nothing"
    assert len(evidence.for_entity("promotion_service", service["id"])) == before
    assert promotion.latest_override(service["id"])["human_override"] == 1
    assert audit.verify_chain()[0] is True

    promotion.set_campaign_promotion(campaign_id, True)
    body = page(client, f"/reach/campaigns/{campaign_id}/promotion")
    assert "Set by manual override — Reviewing their terms" in body


# --- 14. routes ----------------------------------------------------------------

def test_every_promotion_route_lives_under_reach_and_behind_the_gate(client, monkeypatch,
                                                                     campaign_id):
    from reach import web

    rules = [rule.rule for rule in client.application.url_map.iter_rules()
             if "promotion" in rule.rule]
    assert rules
    assert all(rule.startswith("/reach/") for rule in rules)
    endpoints = {rule.endpoint for rule in client.application.url_map.iter_rules()
                 if "promotion" in rule.rule}
    assert not (endpoints & web.ACCESS_EXEMPT)

    swept(monkeypatch)
    promotion.set_campaign_promotion(campaign_id, True)
    service = by_domain("levelpath.example")
    assert "Screened paid promotion options for this release." in page(
        client, f"/reach/campaigns/{campaign_id}/promotion")
    assert service["name"] in page(
        client, f"/reach/promotion/services/{service['id']}?campaign_id={campaign_id}")
    assert "Promotion screening" in page(client, "/reach/promotion/screening")


def test_opening_a_service_is_recorded_and_is_not_a_payment(client, monkeypatch):
    swept(monkeypatch)
    service = by_domain("levelpath.example")
    # A GET the browser can actually follow — a fetch()ed POST could never
    # deliver the person to a cross-origin destination.
    response = client.get(f"/reach/promotion/services/{service['id']}/open")
    assert response.status_code == 302
    assert response.headers["Location"] == service["url"]
    assert client.post(f"/reach/promotion/services/{service['id']}/open").status_code == 405
    assert any(row["action"] == "promotion.opened_external"
               for row in db.query("SELECT action FROM audit_event"))
    assert db.query("SELECT id FROM promotion_plan_item") == []


# --- 15. the ladder is total ----------------------------------------------------

def test_a_service_missing_only_placement_discretion_stays_unknown(monkeypatch):
    with_pages({
        "https://ladder.example/robots.txt": {"status": 200,
                                              "headers": {"Content-Type": "text/plain"},
                                              "body": "User-agent: *\nAllow: /\n"},
        "https://ladder.example/": offer_page(
            "Ladder Promo", "<p>We offer playlist campaigns. Our pricing is $75 per track. "
            "<a href='/terms'>Terms</a></p>"),
        "https://ladder.example/pricing": offer_page(
            "Pricing | Ladder Promo",
            "<p>Our pricing: $75 per track. Refund policy: refunded on request.</p>"),
        "https://ladder.example/terms": offer_page(
            "Terms | Ladder Promo",
            "<p>Ladder Promo Ltd. Curators are vetted and playlists are vetted. "
            "Refund policy: refunded on request. We never use bots.</p>"),
    })
    swept(monkeypatch, ["https://ladder.example/", "https://ladder.example/pricing",
                        "https://ladder.example/terms"])
    service = by_domain("ladder.example")

    assert service["placement_discretionary"] == promotion.TRISTATE_UNKNOWN
    assert service["pricing_min"] == 75.0
    assert service["refund_policy"] == promotion.TRUE
    assert promotion.page_ok_count(service) >= 3
    assert service["screening_status"] == promotion.UNKNOWN
    assert "placement discretion not evidenced" in reasons_of(service)


# --- 16. override precedence ------------------------------------------------------

def test_an_automated_pass_may_tighten_an_override_but_never_loosen_it(monkeypatch):
    swept(monkeypatch)
    service = by_domain("levelpath.example")
    promotion.override_screening(service["id"], promotion.BLOCKED, "Complaint under review")

    promotion.run_screening(service["id"])
    assert promotion.get_service(service["id"])["screening_status"] == promotion.BLOCKED

    # Only another explicit human decision lifts it.
    promotion.override_screening(service["id"], promotion.SCREENED, "Complaint withdrawn")
    assert promotion.get_service(service["id"])["screening_status"] == promotion.SCREENED

    # And an automated pass can still tighten past a human SCREENED override.
    with_pages({"https://levelpath.example/": offer_page(
        "Level Path", "<p>Submission fee $12 per track. Guaranteed placement on Spotify "
        "playlists for every submission.</p>")})
    promotion.request_rescan(service["id"])
    promotion.run_to_completion()
    assert promotion.get_service(service["id"])["screening_status"] == promotion.BLOCKED


# --- 17. the landing page is always rescreened -------------------------------------

def test_a_guarantee_added_to_the_landing_page_is_caught(monkeypatch):
    swept(monkeypatch)
    service = by_domain("levelpath.example")
    assert service["screening_status"] == promotion.SCREENED
    assert service["url"] in promotion.refetch_set(service)

    with_pages({"https://levelpath.example/": offer_page(
        "Level Path — Curated Reviews",
        "<p>There is a submission fee of $14 per track. We now guarantee 10,000 Spotify "
        "streams with every campaign.</p>")})
    promotion.request_rescan(service["id"])
    promotion.run_to_completion()

    refreshed = promotion.get_service(service["id"])
    assert refreshed["screening_status"] == promotion.BLOCKED
    assert refreshed["manual_review_required"] == 1


def test_the_landing_page_is_refetched_even_when_it_backs_no_evidence(monkeypatch):
    """refetch_set must include the declared URLs, not only URLs that happen to
    back a packet — otherwise a bare landing page is never re-read, and a
    guarantee added there is invisible forever."""
    bare = {
        "https://bare.example/robots.txt": {"status": 200,
                                            "headers": {"Content-Type": "text/plain"},
                                            "body": "User-agent: *\nAllow: /\n"},
        "https://bare.example/": offer_page(
            "Bare Promo", "<p>We offer campaigns. <a href='/pricing'>Pricing</a></p>"),
        "https://bare.example/pricing": offer_page(
            "Pricing | Bare Promo",
            "<p>Our pricing is $40 per track. The curator decides. "
            "Refund policy: refunded on request.</p>"),
    }
    with_pages(bare)
    swept(monkeypatch, ["https://bare.example/", "https://bare.example/pricing"])
    service = by_domain("bare.example")

    # The declared URLs are in the set because they are declared, not because
    # they happen to back a packet: deleting that block has to fail here.
    db.update("promotion_service", service["id"],
              {"terms_url": "https://bare.example/terms-never-read",
               "submission_url": "https://bare.example/submit-never-read"})
    refetched = promotion.refetch_set(promotion.get_service(service["id"]))
    backing = {row["source_url"] for row in promotion.current_packets(service["id"])}
    for declared in ("https://bare.example/", "https://bare.example/terms-never-read",
                     "https://bare.example/submit-never-read"):
        assert declared in refetched
    assert {"https://bare.example/terms-never-read",
            "https://bare.example/submit-never-read"} & backing == set()

    with_pages(dict(bare, **{"https://bare.example/": offer_page(
        "Bare Promo",
        "<p>We offer campaigns from $40. We guarantee 25,000 Spotify streams.</p>")}))
    promotion.request_rescan(service["id"])
    promotion.run_to_completion()
    assert promotion.get_service(service["id"])["screening_status"] == promotion.BLOCKED


def test_a_single_page_service_cannot_be_screened(monkeypatch):
    """The two-page rung: one page is not enough to describe a business."""
    single = {
        "https://onepage.example/robots.txt": {"status": 200,
                                               "headers": {"Content-Type": "text/plain"},
                                               "body": "User-agent: *\nAllow: /\n"},
        "https://onepage.example/": offer_page(
            "One Page Promo",
            "<p>One Page Promo Ltd. We offer playlist pitching at $30 per track. "
            "The curator decides. Refund policy: refunded on request. "
            "Curators are vetted and playlists are vetted. Contact: "
            "<a href='https://onepage.example/'>home</a></p>"),
    }
    with_pages(single)
    swept(monkeypatch, ["https://onepage.example/"])
    service = by_domain("onepage.example")

    assert promotion.page_ok_count(service) == 1
    assert service["screening_status"] == promotion.UNKNOWN
    assert "fewer than two pages of this service could be read" in reasons_of(service)


# --- 18. definite flips and conflicts ------------------------------------------------

def test_a_definite_value_flips_on_fresh_evidence_from_the_same_page(monkeypatch):
    swept(monkeypatch)
    service = by_domain("levelpath.example")
    assert service["placement_discretionary"] == promotion.TRUE

    with_pages({"https://levelpath.example/terms": offer_page(
        "Terms | Level Path",
        "<p>Level Path Reviews Ltd. Every paying artist is guaranteed placement on one "
        "of our playlists. Submission fee $14.</p>")})
    promotion.request_rescan(service["id"])
    promotion.run_to_completion()

    refreshed = promotion.get_service(service["id"])
    assert refreshed["guaranteed_playlist_placement"] == promotion.TRUE
    assert refreshed["screening_status"] == promotion.BLOCKED


def test_equally_confident_opposite_claims_leave_the_field_unknown(monkeypatch):
    with_pages({
        "https://conflict.example/robots.txt": {"status": 200,
                                                "headers": {"Content-Type": "text/plain"},
                                                "body": "User-agent: *\nAllow: /\n"},
        "https://conflict.example/": offer_page(
            "Conflict Promo",
            "<p>We offer campaigns at $60 per track. Curators are paid per placement.</p>"),
        "https://conflict.example/terms": offer_page(
            "Terms | Conflict Promo",
            "<p>Conflict Promo Ltd. Curators are never paid for their picks.</p>"),
    })
    swept(monkeypatch, ["https://conflict.example/", "https://conflict.example/terms"])
    service = by_domain("conflict.example")

    assert service["curator_compensation"] == promotion.TRISTATE_UNKNOWN
    assert service["manual_review_required"] == 1
    urls = {row["source_url"] for row in promotion.current_packets(service["id"])}
    assert len(urls) >= 2, "both receipts are kept"


# --- 19. negation and aboutness --------------------------------------------------

def test_a_denial_is_never_read_as_an_offer(monkeypatch):
    swept(monkeypatch)
    service = by_domain("honestpromo.example")
    assert service is not None
    assert service["screening_status"] != promotion.BLOCKED
    assert service["placement_discretionary"] == promotion.TRUE
    assert not [row for row in promotion.current_packets(service["id"])
                if row["supports_field"] == "stream_guarantees"]


def test_an_extractor_guarantee_classification_on_negated_text_cannot_block():
    sanitized = sanitize_page("https://honestpromo.example/")
    assert extractor.classify(sanitized)["classification"] in (
        extractor.GUARANTEED_STREAMS, extractor.GUARANTEED_PLACEMENT)
    # extractor has no negation guard by design; re-validation is what stops it.
    assert promotion.corroborating_classification(sanitized) == []


def test_an_article_about_services_creates_no_service(monkeypatch):
    swept(monkeypatch)
    assert promotion.service_by_domain("promoreview.example") is None
    documents = db.query("SELECT id FROM source_document WHERE domain = ?",
                         ("promoreview.example",))
    assert documents, "the page is still on record as a source document"


# --- 20. score honesty --------------------------------------------------------

def test_an_all_unknown_component_set_stores_a_null_score():
    service_id = promotion.ensure_service("Blank", "blank.example", None)
    promotion.run_screening(service_id)
    row = promotion.latest_screening(service_id)
    assert row["score"] is None, "no component known means NULL, never 0"
    assert row["status"] == promotion.UNKNOWN


# --- 22. the review flag resolves ------------------------------------------------

def test_the_review_flag_can_be_cleared_and_stays_cleared(monkeypatch):
    swept(monkeypatch)
    service = by_domain("levelpath.example")
    db.update("promotion_service", service["id"], {"manual_review_required": 1})
    promotion.run_screening(service["id"])
    assert promotion.get_service(service["id"])["screening_status"] == promotion.CAUTION

    promotion.resolve_review(service["id"])
    assert promotion.get_service(service["id"])["manual_review_required"] == 0
    promotion.run_screening(service["id"])
    refreshed = promotion.get_service(service["id"])
    assert refreshed["manual_review_required"] == 0
    assert refreshed["screening_status"] == promotion.SCREENED


# --- 23. the suggested allocation -------------------------------------------------

def test_the_allocation_splits_what_is_left_and_states_its_basis(client, planned):
    # Two screened services in different categories, so "evenly" is exercised
    # rather than asserted over a single bucket.
    for service_id, types in ((by_domain("levelpath.example")["id"],
                               ["CURATOR_SUBMISSIONS"]),):
        db.update("promotion_service", service_id, {"service_types_json": json.dumps(types)})
    radio_id = promotion.ensure_service("Airwave Promo", "airwave.example",
                                        "https://airwave.example/")
    db.update("promotion_service", radio_id, {
        "screening_status": promotion.SCREENED,
        "service_types_json": json.dumps(["RADIO"]),
    })
    promotion.compute_fits(planned)

    allocation = promotion.suggested_allocation(planned)
    assert allocation is not None
    categories = [entry["category"] for entry in allocation["categories"]]
    assert categories == ["CURATOR_SUBMISSIONS", "RADIO_PROMOTION"]
    # The literal sentence, not a re-derivation of the constant: rewriting
    # ALLOCATION_BASIS to a false claim has to fail here.
    assert allocation["basis"] == (
        "Split evenly across the 2 categories where REACH found at least one "
        "screened service with a computed campaign fit.")
    assert page(client, f"/reach/campaigns/{planned}/promotion").count(
        allocation["basis"]) == 1
    amounts = {entry["amount"] for entry in allocation["categories"]}
    assert len(amounts) == 1, "an even split gives every category the same amount"
    total = sum(entry["amount"] for entry in allocation["categories"])
    assert round(total) == round(allocation["remaining"])


def test_the_allocation_says_when_unpriced_items_are_not_subtracted(planned):
    promotion.compute_fits(planned)
    promotion.add_plan_item(planned, label="Manager retainer", category="OTHER")
    allocation = promotion.suggested_allocation(planned)
    assert allocation["caveat"] == (
        "1 planned item have no amount and are not subtracted.")

    # Dismissing the only screened service stops its category qualifying.
    for service in promotion.services():
        if service["screening_status"] == promotion.SCREENED:
            promotion.dismiss(service["id"], planned)
    assert promotion.suggested_allocation(planned) is None


def test_no_allocation_panel_without_a_known_remaining(monkeypatch, campaign_id):
    swept(monkeypatch)
    promotion.set_campaign_promotion(campaign_id, True)
    promotion.compute_fits(campaign_id)
    assert promotion.suggested_allocation(campaign_id) is None


def test_a_dismissed_service_leaves_the_campaign_views(monkeypatch, campaign_id):
    swept(monkeypatch)
    promotion.set_campaign_promotion(campaign_id, True)
    service = by_domain("levelpath.example")
    promotion.dismiss(service["id"], campaign_id)
    domains = {item["service"]["canonical_domain"]
               for item in promotion.campaign_services(campaign_id)}
    assert "levelpath.example" not in domains
    assert db.query("SELECT id FROM promotion_plan_item WHERE campaign_id = ?",
                    (campaign_id,)) == []


# --- 24. vetting caution and the external-open copy --------------------------------

def test_vetting_is_load_bearing_for_a_marketplace_only(monkeypatch):
    marketplace = {
        "https://roster.example/robots.txt": {"status": 200,
                                              "headers": {"Content-Type": "text/plain"},
                                              "body": "User-agent: *\nAllow: /\n"},
        "https://roster.example/": offer_page(
            "Roster Promo",
            "<p>Our network of curators reviews every track. We offer submissions at $20 "
            "per track. The curator decides. <a href='/terms'>Terms</a></p>"),
        "https://roster.example/terms": offer_page(
            "Terms | Roster Promo",
            "<p>Roster Promo Ltd. The curator decides what gets added. "
            "Refund policy: refunded on request.</p>"),
    }
    with_pages(marketplace)
    swept(monkeypatch, list(marketplace)[1:])
    service = by_domain("roster.example")
    assert service["category"] == promotion.PLATFORM
    assert service["curator_vetting"] == promotion.TRISTATE_UNKNOWN
    assert service["screening_status"] == promotion.CAUTION
    assert any("vetting" in reason for reason in reasons_of(service))

    # The same gap on an individual curator is not load-bearing.
    individual = by_domain("levelpath.example")
    assert individual is None or individual["curator_vetting"] == promotion.TRISTATE_UNKNOWN


def test_leaving_reach_copy_renders_on_card_and_dossier(client, monkeypatch, campaign_id):
    swept(monkeypatch)
    promotion.set_campaign_promotion(campaign_id, True)
    service = by_domain("levelpath.example")

    card = page(client, f"/reach/campaigns/{campaign_id}/promotion")
    assert promotion.LEAVING_REACH in card
    assert promotion.LEAVING_REACH_DETAIL in card
    dossier = page(client, f"/reach/promotion/services/{service['id']}?campaign_id={campaign_id}")
    assert promotion.LEAVING_REACH in dossier
    assert promotion.LEAVING_REACH_DETAIL in dossier
    # Never a storefront.
    for word in ("Buy Placement", "Buy Streams", "Add to cart"):
        assert word not in card and word not in dossier


def test_the_screened_badge_never_says_verified(client, monkeypatch, campaign_id):
    swept(monkeypatch)
    promotion.set_campaign_promotion(campaign_id, True)
    body = page(client, f"/reach/campaigns/{campaign_id}/promotion")
    assert "REACH SCREENED" in body
    assert "VERIFIED" not in body.upper().replace("UNVERIFIED", "")
    assert promotion.ENDORSEMENT_DISCLAIMER in body


# --- review fixes: the sweep reaches SCREENED, and no number is invented ----------

def test_an_unstubbed_fixture_sweep_reaches_screened(complete_profile):
    """The sweep has to follow a service's own terms and pricing links.

    Refund policy and placement discretion live on those pages, so a sweep that
    only reads the page a search returned can never screen anything — every
    legitimate service would sit at CAUTION forever.
    """
    promotion.start_sweep()
    promotion.run_to_completion()
    services = promotion.services()
    assert services
    screened = [s for s in services if s["screening_status"] == promotion.SCREENED]
    assert screened, "an unstubbed fixture-mode sweep produces at least one SCREENED service"

    service = screened[0]
    assert promotion.page_ok_count(service) >= 2
    urls = {row["source_url"] for row in promotion.current_packets(service["id"])}
    assert len(urls) >= 2, "the sweep followed the service's own declared pages"


def test_a_score_is_withheld_when_too_little_was_measured(monkeypatch):
    """A renormalized average over one known component is that component
    wearing a percent sign. UNKNOWN must not arrive at 100."""
    swept(monkeypatch)
    for service in promotion.services():
        row = promotion.latest_screening(service["id"])
        coverage = promotion._with_coverage(row)["coverage"]
        if service["screening_status"] == promotion.UNKNOWN and coverage < 50:
            assert row["score"] is None, (
                f"{service['canonical_domain']} scored {row['score']} on {coverage}% coverage")

    # Freshness alone can never carry a score: it says when REACH read the
    # pages, not what they said.
    facts = {"fact_freshness": {"company_identity": True}}
    assert promotion._score(facts, []) is None
    assert promotion._components(facts, [])["evidence_freshness"] == 1.0

    # And a couple of substantive components is still not enough to publish a
    # number: identity + terms + freshness is a quarter of the weight, and
    # renormalizing that to 100 is the exact defect this floor exists to stop.
    thin = {"name": "Thin Promo", "terms_url": "https://thin.example/terms",
            "fact_freshness": {"company_identity": True, "terms": True}}
    assert promotion.score_coverage(promotion._components(thin, [])) < promotion.MIN_SCORE_COVERAGE
    assert promotion._score(thin, []) is None

    full = dict(thin, business_model_summary="A curator reviewing submissions.",
                pricing_min=10.0, placement_discretionary=promotion.TRUE,
                anti_bot_policy=promotion.TRUE, refund_policy=promotion.TRUE,
                curator_vetting=promotion.TRUE)
    assert promotion.score_coverage(promotion._components(full, [])) >= promotion.MIN_SCORE_COVERAGE
    assert promotion._score(full, []) == 100


def test_an_unknown_service_is_never_told_it_has_no_guarantees(client, monkeypatch,
                                                               campaign_id):
    """The tri-states have no FALSE writer, so `!= 'TRUE'` was `== 'UNKNOWN'`:
    the dossier reassured the reader about a service it could not read."""
    swept(monkeypatch)
    promotion.set_campaign_promotion(campaign_id, True)
    service = by_domain("gatedpromo.example")
    assert service["screening_status"] == promotion.UNKNOWN
    assert service["guaranteed_playlist_placement"] == promotion.TRISTATE_UNKNOWN

    body = page(client, f"/reach/promotion/services/{service['id']}?campaign_id={campaign_id}")
    assert "Playlist placement is not guaranteed." not in body
    assert "Streaming volume is not guaranteed." not in body
    assert "REACH found no guarantee claims in the pages it could read" in body
    assert promotion.UNKNOWN_STATEMENT in body


def test_a_card_can_add_any_service_type_to_the_plan(planned):
    """Cards hand add_plan_item the service's own type; it translates. Six of
    nine types used to raise "Unknown allocation category"."""
    for service_type, expected in promotion.SERVICE_TYPE_TO_ALLOCATION.items():
        item_id = promotion.add_plan_item(planned, label=f"Test {service_type}",
                                          category=service_type, amount=1)
        row = db.query_one("SELECT * FROM promotion_plan_item WHERE id = ?", (item_id,))
        assert row["allocation_category"] == expected

    # An allocation category still passes through, and nonsense is still refused.
    item_id = promotion.add_plan_item(planned, label="Direct", category="PRESS_PR")
    assert db.query_one("SELECT allocation_category FROM promotion_plan_item WHERE id = ?",
                        (item_id,))["allocation_category"] == "PRESS_PR"
    with pytest.raises(ValidationError):
        promotion.add_plan_item(planned, label="Nope", category="NOT_A_CATEGORY")


def test_a_paid_media_service_can_be_added_from_its_card(client, planned):
    service = by_domain("presswire.example")
    response = client.post(f"/reach/promotion/services/{service['id']}/plan",
                           json={"campaign_id": planned, "category": "ADVERTISING",
                                 "amount": "250"})
    assert response.get_json()["ok"] is True


def test_an_unusable_page_is_audited_like_radar_does(monkeypatch):
    swept(monkeypatch, ["https://cfshield.example/submit"])
    assert promotion.services() == []
    assert any(row["action"] == "promotion.page_unusable"
               for row in db.query("SELECT action FROM audit_event"))


def test_superseded_evidence_is_marked_as_such(monkeypatch):
    swept(monkeypatch)
    service = by_domain("levelpath.example")
    with_pages({"https://levelpath.example/terms": offer_page(
        "Terms | Level Path",
        "<p>Level Path Reviews Ltd. The curator decides. Rewritten this week.</p>")})
    promotion.request_rescan(service["id"])
    promotion.run_to_completion()

    items = promotion.evidence_view(service["id"])
    assert any(item["superseded"] for item in items), "the replaced read is marked"
    assert any(not item["superseded"] for item in items)


# --- the sweep runs against the real fixture corpus too ---------------------------

def test_the_sweep_works_end_to_end_in_fixture_mode(complete_profile):
    promotion.start_sweep()
    promotion.run_to_completion()
    assert promotion.state()["last_sweep_finished_at"] is not None
    assert promotion.state()["searches_used"] <= promotion.SWEEP_SEARCH_CAP
    results = db.query("SELECT result_json FROM job_run WHERE kind = 'PROMO_SEARCH' "
                       "AND result_json IS NOT NULL LIMIT 1")
    assert results and "FIXTURE" in results[0]["result_json"]


def test_query_planning_uses_only_supplied_profile_values(complete_profile):
    queries = [item["query"] for item in promotion.plan_queries()]
    assert queries
    assert any("dark electronic" in query for query in queries)
    assert all("{" not in query for query in queries)
    assert any(name in query for name in promotion.SEED_SERVICES for query in queries)
    assert len(queries) <= promotion.SWEEP_SEARCH_CAP


def test_seeded_names_are_never_pre_created_or_pre_trusted():
    assert promotion.services() == []
    for name in promotion.SEED_SERVICES:
        assert not db.query("SELECT id FROM promotion_service WHERE name = ?", (name,))


def test_platform_domains_are_never_screened_as_services(monkeypatch):
    swept(monkeypatch, ["https://www.tiktok.com/@promo", "https://open.spotify.com/playlist/x",
                        "https://levelpath.example/"])
    domains = {service["canonical_domain"] for service in promotion.services()}
    assert domains == {"levelpath.example"}


def test_a_blocked_fetch_is_recorded_as_a_refusal_not_a_failure(monkeypatch):
    swept(monkeypatch, ["https://norobots.example/private/contacts"])
    row = db.query_one("SELECT * FROM source_document WHERE domain = ?",
                       ("norobots.example",))
    assert row is not None
    assert row["fetch_status"] == evidence.FETCH_BLOCKED
    assert row["block_reason"]
    assert promotion.services() == []


def test_promotion_jobs_are_registered_and_cancellable(monkeypatch):
    for kind in ("PROMO_SWEEP", "PROMO_SEARCH", "PROMO_FETCH", "PROMO_SCREEN"):
        assert kind in jobs.JOB_KINDS
        assert jobs.handler_for(kind) is not None

    use_search(monkeypatch, SERVICE_URLS)
    promotion.start_sweep()
    jobs.run_pending(limit=1)
    pending = db.query("SELECT id FROM job_run WHERE kind LIKE 'PROMO%' AND status = 'PENDING'")
    assert pending
    for row in pending:
        jobs.cancel(row["id"])
    promotion.run_to_completion()
    assert promotion.services() == []


def test_evidence_packets_carry_a_content_hash_for_change_detection(monkeypatch):
    swept(monkeypatch)
    service = by_domain("levelpath.example")
    packets = evidence.for_entity("promotion_service", service["id"])
    assert packets
    assert all(row["content_hash"] for row in packets)
    assert all(row["extractor_version"] == promotion.PROMO_VERSION for row in packets)


def test_the_business_model_summary_is_assembled_only_from_receipts(monkeypatch):
    swept(monkeypatch)
    service = by_domain("levelpath.example")
    summary = service["business_model_summary"]
    assert summary
    assert "14" in summary
    assert "curator's discretion" in summary
    blank_id = promotion.ensure_service("Blank", "blank2.example", None)
    assert promotion.business_model_summary(promotion.get_service(blank_id)) is None


def test_screening_history_is_append_only(monkeypatch):
    swept(monkeypatch)
    service = by_domain("levelpath.example")
    before = len(promotion.screening_history(service["id"]))
    promotion.run_screening(service["id"])
    after = promotion.screening_history(service["id"])
    assert len(after) == before + 1
    assert after[0]["created_at"] >= after[-1]["created_at"]


def test_reserved_support_fields_are_declared_but_unused(monkeypatch):
    swept(monkeypatch)
    assert promotion.RESERVED_SUPPORTS == ("complaints_enforcement", "third_party_report")
    used = {row["supports_field"] for row in db.query(
        "SELECT DISTINCT supports_field FROM evidence_packet WHERE entity_type = ?",
        ("promotion_service",))}
    assert not (used & set(promotion.RESERVED_SUPPORTS))


def test_the_screening_projection_is_json_safe_and_complete(monkeypatch):
    swept(monkeypatch)
    service = by_domain("levelpath.example")
    facts = promotion.screening_inputs(service)
    json.dumps(facts)
    for field in promotion.TRISTATE_FIELDS:
        assert field in facts
