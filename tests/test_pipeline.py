"""Campaign, discovery, provider-state and outcome tests.

Covers Phase One scenarios 1–3, 13–17 and 31–33.
"""

import pytest

from reach import (analytics, approvals, campaigns, catalog, clock, drafts, evidence,
                   jobs, outcomes, pipeline, policy, profile, scoring, sender)
from reach.errors import (ProviderNotConnected, ProviderRateLimited, QuotaExceeded,
                          ValidationError)
from reach.providers import base as provider_base
from reach.providers import search as search_provider
from reach.providers import spotify as spotify_provider
from reach.providers import youtube as youtube_provider

from .conftest import any_live_target, first_ready_target, target_named


# --- 1–3: campaign creation from a real stored track -----------------------

def test_a_real_stored_track_can_create_a_campaign(recording, attested):
    campaign_id = campaigns.create(recording["id"], mode=campaigns.SCOUT,
                                   territories=["DE"])
    campaign = campaigns.get(campaign_id)
    assert campaign["recording_id"] == recording["id"]
    assert campaign["rights_attestation_id"] is not None
    assert catalog.track(recording).title == "Midnight Drive"


def test_campaign_requires_a_rights_attestation(recording, complete_profile):
    with pytest.raises(ValidationError) as info:
        campaigns.create(recording["id"])
    assert "rights attestation" in str(info.value)


def test_missing_track_metadata_is_surfaced_not_guessed():
    velvet = catalog.recording_by_slug("velvet-static")
    profile_id = profile.get_or_create(velvet["id"])
    completeness = profile.completeness(profile_id)

    assert completeness["ready"] is False
    assert "primary_genre" in completeness["missing_required"]

    fields = {item["field"]: item for item in profile.fields(profile_id)}
    assert fields["bpm"]["display"] == "UNKNOWN"
    assert fields["bpm"]["confidence"] == 0.0
    assert fields["comparable_artists"]["value"] is None


def test_incomplete_profile_blocks_campaign_creation():
    velvet = catalog.recording_by_slug("velvet-static")
    catalog.attest_rights(velvet["id"])
    with pytest.raises(ValidationError) as info:
        campaigns.create(velvet["id"])
    assert "Track intelligence profile is incomplete" in str(info.value)


def test_profile_confidence_and_provenance_persist(recording, complete_profile):
    fields = {item["field"]: item for item in profile.fields(complete_profile)}
    genre = fields["primary_genre"]
    assert genre["value"] == "dark electronic"
    assert genre["source"] == profile.SOURCE_USER
    assert genre["confidence"] == 1.0
    assert genre["human_override"] is True
    assert genre["extractor_version"] == profile.EXTRACTOR_VERSION
    assert genre["generated_at"] is not None


def test_release_readiness_states_what_each_gap_gates(recording, attested):
    readiness = catalog.release_readiness(recording["id"])
    keys = {check["key"] for check in readiness["checks"]}
    assert {"isrc", "upc", "distribution", "rights"} <= keys
    for check in readiness["checks"]:
        assert check["consequence"]


def test_versions_are_distinct_recordings(recording):
    remix_id = catalog.add_version(recording["id"], "REMIX", mix_name="Night Shift Remix")
    assert remix_id != recording["id"]
    remix = catalog.get_recording(remix_id)
    assert remix["version_type"] == "REMIX"
    assert remix["master_version"] == 0
    identifiers = catalog.identifiers(remix_id)
    assert identifiers["mix_name"] == "Night Shift Remix"


# --- discovery --------------------------------------------------------------

def test_query_planning_uses_only_supplied_profile_values(campaign_id):
    queries = pipeline.plan_queries(campaign_id)
    assert queries
    joined = " ".join(query["query"] for query in queries)
    assert "dark electronic" in joined
    assert "{" not in joined  # no unfilled template placeholders
    assert any(query["family"] == "LOCAL_LANGUAGE" for query in queries)
    assert any("Musik einreichen" in query["query"] for query in queries)


def test_discovery_produces_targets_with_evidence(scouted):
    targets = campaigns.targets(scouted)
    assert len(targets) >= 8
    live = [row for row in targets if row["status"] != campaigns.DUPLICATE]
    for row in live[:5]:
        packets = evidence.summary("outlet", row["outlet_id"])
        assert packets, f"{row['outlet_name']} has no evidence"
        assert all(item["source_url"] for item in packets)


def test_every_qualified_target_has_a_score_and_a_risk_assessment(scouted):
    for row in campaigns.targets(scouted):
        if row["status"] == campaigns.DUPLICATE:
            continue
        assert scoring.latest_score(row["id"]) is not None
        assert scoring.latest_risk(row["id"]) is not None


def test_score_reasons_are_generated_from_components(scouted_without_sender):
    import json

    target = any_live_target(scouted_without_sender)
    score = scoring.latest_score(target["id"])
    reasons = json.loads(score["reasons_json"])
    assert reasons
    assert all(reason["sign"] in "+-?" for reason in reasons)
    components = json.loads(score["components_json"])
    assert set(components) == set(scoring.COMPONENT_WEIGHTS)


def test_unknown_components_are_reported_as_unknown_not_zero(scouted):
    import json

    for row in campaigns.targets(scouted):
        score = scoring.latest_score(row["id"])
        if score is None:
            continue
        components = json.loads(score["components_json"])
        assert any(value is None for value in components.values()), \
            "an all-known score means UNKNOWN was silently coerced"
        break


def test_score_override_requires_a_reason(scouted_without_sender):
    target = any_live_target(scouted_without_sender)
    with pytest.raises(ValidationError):
        scoring.override_score(target["id"], 90, "")
    scoring.override_score(target["id"], 90, "Known relationship with the programmer")
    assert scoring.latest_score(target["id"])["human_override"] == 1


def test_closed_submissions_are_disqualified(scouted):
    target = target_named(scouted, "dormantwaves")
    if target is None:
        pytest.skip("stale fixture not surfaced by this campaign's queries")
    assert target["status"] in (campaigns.DISQUALIFIED, campaigns.DUPLICATE)


# --- 13: staleness ----------------------------------------------------------

def test_stale_sources_are_identified_for_reverification(scouted):
    documents = evidence.stale_documents()
    assert documents == []  # freshly fetched

    from reach import db

    db.execute("UPDATE source_document SET stale_at = ? WHERE fetch_status = 'OK'",
               (clock.days_from_now(-1),))
    assert len(evidence.stale_documents()) > 0


def test_evidence_marks_verification_and_corroboration(scouted_without_sender):
    target = any_live_target(scouted_without_sender)
    packets = evidence.for_entity("outlet", target["outlet_id"], "classification")
    assert packets
    evidence.mark_verified(packets[0]["id"])
    refreshed = evidence.get(packets[0]["id"])
    assert refreshed["verified_at"] is not None
    assert evidence.corroboration("outlet", target["outlet_id"], "classification") >= 1


# --- 14–15: rate limits and quota ------------------------------------------

def test_429_response_respects_retry_after(monkeypatch):
    class RateLimitedTransport:
        def request(self, validated, headers):
            return 429, {"Retry-After": "42"}, b"{}"

    from reach import fetcher

    monkeypatch.setenv("REACH_YOUTUBE_API_KEY", "test-key")
    fetcher.set_transport(RateLimitedTransport())
    with pytest.raises(ProviderRateLimited) as info:
        provider_base.get_json("https://www.googleapis.com/youtube/v3/search",
                               provider="youtube", operation="search")
    assert info.value.retry_after == 42


def test_rate_limited_job_is_retried_with_backoff(campaign_id, monkeypatch):
    @jobs.register("REFRESH_SOURCE")
    def _raise(context):
        raise ProviderRateLimited("slow down", retry_after=30)

    job_id = jobs.enqueue("REFRESH_SOURCE", {"x": 1}, campaign_id=campaign_id)
    jobs.run_one()
    job = jobs.get(job_id)
    assert job["status"] == jobs.PENDING
    assert job["error_kind"] == "PROVIDER_RATE_LIMITED"
    assert job["next_attempt_at"] > clock.now_iso()


def test_quota_warning_appears_before_exhaustion(monkeypatch):
    monkeypatch.setenv("REACH_YOUTUBE_API_KEY", "test-key")
    policy.reset_quota("youtube")
    state = policy.check_quota("youtube", "search")
    assert state["warn"] is False

    policy.record_request("youtube", "search", "OK", cost_units=8500)
    state = policy.check_quota("youtube", "search")
    assert state["warn"] is True

    policy.record_request("youtube", "search", "OK", cost_units=1500)
    with pytest.raises(QuotaExceeded):
        policy.check_quota("youtube", "search")


def test_youtube_quota_costs_are_priced_per_operation():
    assert policy.estimate_quota_cost("youtube", "search") == 100
    assert policy.estimate_quota_cost("youtube", "channels.list") == 1
    report = youtube_provider.quota_report()
    assert report["search_cost"] == 100


# --- 16–17: honest provider state ------------------------------------------

def test_unavailable_provider_reports_not_connected():
    state = youtube_provider.status()
    assert state["state"] == policy.NOT_CONNECTED
    assert "REACH_YOUTUBE_API_KEY" in state["missing_credentials"]
    assert youtube_provider.connected() is False


def test_missing_credentials_raise_instead_of_returning_fake_results():
    with pytest.raises(ProviderNotConnected):
        youtube_provider.search_channels("dark electronic")
    with pytest.raises(ProviderNotConnected):
        spotify_provider.find_track(isrc="USRC12345678")


def test_search_without_credentials_is_labelled_as_a_fixture_run():
    response = search_provider.search("dark electronic playlist submissions")
    assert response.mode == provider_base.FIXTURE
    assert response.is_live is False
    assert "fixture corpus" in response.note


def test_manual_only_providers_refuse_automated_capability():
    from reach.errors import PolicyViolation

    with pytest.raises(PolicyViolation):
        policy.require_capability("apple_music", policy.EDITORIAL_PITCH)
    with pytest.raises(PolicyViolation):
        policy.require_capability("deezer", policy.CATALOG_SEARCH)
    with pytest.raises(PolicyViolation):
        policy.require_capability("chartmetric", policy.CATALOG_SEARCH)


def test_unregistered_provider_is_blocked_by_default():
    from reach.errors import PolicyViolation

    unknown = policy.get("some_unknown_vendor")
    assert unknown.status == policy.BLOCKED
    with pytest.raises(PolicyViolation):
        policy.require_capability("some_unknown_vendor", policy.CATALOG_SEARCH)


def test_spotify_policy_forbids_model_use_and_contact_resolution():
    spotify_policy = policy.get("spotify")
    assert spotify_policy.maySendDataToLanguageModel is False
    assert spotify_policy.mayCreateEmbeddings is False
    assert spotify_policy.mayUseForModelTraining is False
    assert spotify_policy.mayResolveContactsFromProviderData is False
    assert spotify_policy.webDiscoveryAllowed is False
    assert spotify_policy.authenticatedBrowserAutomationAllowed is False


def test_spotify_editorial_pitch_is_declared_manual():
    requirements = spotify_provider.editorial_pitch_requirements()
    assert requirements["automation_available"] is False
    assert "no submission api" in requirements["reason"].lower()


def test_provider_policy_review_dates_are_tracked():
    state = policy.connection_state("youtube")
    assert state["last_verified_at"]
    assert state["next_review_at"]
    assert state["review_due"] is False


# --- durable jobs -----------------------------------------------------------

def test_jobs_are_idempotent_by_key(campaign_id):
    first = jobs.enqueue("UPDATE_ANALYTICS", {"a": 1}, idempotency_key="same",
                         campaign_id=campaign_id)
    second = jobs.enqueue("UPDATE_ANALYTICS", {"a": 1}, idempotency_key="same",
                          campaign_id=campaign_id)
    assert first == second


def test_cancelled_jobs_do_not_run(campaign_id):
    pipeline.start(campaign_id)
    cancelled = jobs.cancel_campaign_jobs(campaign_id)
    assert cancelled >= 1
    assert jobs.run_one() is None


def test_failed_jobs_dead_letter_after_max_attempts(campaign_id):
    @jobs.register("SANITIZE_CONTENT")
    def _always_fail(context):
        raise RuntimeError("boom")

    job_id = jobs.enqueue("SANITIZE_CONTENT", {"x": 1}, campaign_id=campaign_id,
                          max_attempts=2)
    for _ in range(4):
        clock.freeze(clock.parse(clock.days_from_now(1)))
        jobs.run_one()
    clock.unfreeze()
    assert jobs.get(job_id)["status"] == jobs.DEAD_LETTER


def test_job_progress_is_reported_for_a_campaign(scouted):
    progress = jobs.progress_for_campaign(scouted)
    assert progress["total"] > 0
    assert progress["complete"] is True
    assert progress["pct"] == 100


def test_diminishing_returns_stop_rule_reports_a_reason(scouted):
    reason = pipeline.should_stop(scouted)
    assert reason is None or isinstance(reason, str)

    from reach import db

    campaign = campaigns.get(scouted)
    db.execute("UPDATE campaign_budget SET searches_used = ? WHERE campaign_id = ?",
               (campaign["search_budget"], scouted))
    assert "budget" in pipeline.should_stop(scouted).lower()


# --- 31–33: placements, acceptance, metrics --------------------------------

def test_placement_cannot_exist_without_evidence(scouted, sender_ready):
    target = first_ready_target(scouted)
    with pytest.raises(ValidationError):
        outcomes.record_placement(target["id"], "MADE_IT_UP")
    with pytest.raises(ValidationError):
        outcomes.record_placement(target["id"], outcomes.EVIDENCE_URL, url=None)


def test_acceptance_does_not_become_a_placement(scouted, sender_ready):
    target = first_ready_target(scouted)
    drafts.generate(target["id"])
    approvals.approve(target["id"])
    approvals.send_approved(target["id"])
    outcomes.record_response(target["id"], outcomes.ACCEPT)

    assert campaigns.get_target(target["id"])["status"] == campaigns.ACCEPTED
    assert outcomes.placements(scouted) == []
    assert analytics.campaign_metrics(scouted)["placed"] == 0


def test_recorded_placement_carries_its_evidence(scouted, sender_ready):
    target = first_ready_target(scouted)
    placement_id = outcomes.record_placement(
        target["id"], outcomes.EVIDENCE_URL, url="https://bassforge.example/promo",
        program_name="Bassforge Weekly",
    )
    packet = outcomes.placement_evidence(placement_id)
    assert packet is not None
    assert packet["source_type"] == outcomes.EVIDENCE_URL
    assert packet["verified_at"] is not None
    assert campaigns.get_target(target["id"])["status"] == campaigns.PLACED


def test_campaign_metrics_match_persisted_records(scouted, sender_ready):
    from reach import db

    target = first_ready_target(scouted)
    drafts.generate(target["id"])
    approvals.approve(target["id"])
    approvals.send_approved(target["id"])
    outcomes.record_response(target["id"], outcomes.ACCEPT)
    outcomes.record_placement(target["id"], outcomes.EVIDENCE_CURATOR,
                              url="https://bassforge.example/promo")

    metrics = analytics.campaign_metrics(scouted)

    discovered = db.query_one(
        "SELECT COUNT(*) AS n FROM campaign_target WHERE campaign_id = ?", (scouted,)
    )["n"]
    submitted = db.query_one(
        "SELECT COUNT(*) AS n FROM submission s JOIN campaign_target t ON t.id = s.target_id "
        "WHERE t.campaign_id = ? AND s.sent_at IS NOT NULL", (scouted,)
    )["n"]
    placed = db.query_one(
        "SELECT COUNT(*) AS n FROM placement WHERE campaign_id = ? AND removed_date IS NULL",
        (scouted,)
    )["n"]

    assert metrics["discovered"] == discovered
    assert metrics["submitted"] == submitted
    assert metrics["placed"] == placed


def test_placement_value_reports_unmeasured_rather_than_zero(scouted):
    target = first_ready_target(scouted)
    placement_id = outcomes.record_placement(
        target["id"], outcomes.EVIDENCE_USER, url="https://bassforge.example/promo")
    value = analytics.placement_value(placement_id)
    assert value["state"] == "UNMEASURED"
    assert value["score"] is None


def test_attribution_never_claims_causation(scouted):
    analytics.capture_baseline(scouted)
    rows = analytics.attribution_report(scouted)
    assert rows
    for row in rows:
        assert row["state"] in ("MEASURED", "NO_BASELINE", "NO_OUTCOME")
        assert "note" in row
        if row["state"] == "MEASURED":
            assert row["attribution"] == analytics.OBSERVED_CORRELATION


def test_baseline_is_captured_from_first_party_data(scouted):
    analytics.capture_baseline(scouted)
    snapshots = analytics.snapshots(scouted, kind=analytics.BASELINE)
    assert snapshots
    assert all(row["source"] == "reach_catalog" for row in snapshots)


# --- sender health ----------------------------------------------------------

def test_sender_health_fails_without_configuration():
    health = sender.health_summary()
    assert health["ready"] is False
    assert health["status"] == sender.DISABLED
    assert health["failing"]


def test_sender_health_passes_when_configured(sender_ready):
    health = sender.health_summary()
    assert health["ready"] is True
    assert health["status"] == sender.READY
    states = {check["key"]: check["state"] for check in health["checks"]}
    assert states["spf"] == sender.PASS
    assert states["dkim"] == sender.PASS
    assert states["dmarc"] == sender.PASS
    assert states["unsubscribe"] == sender.PASS


def test_absent_dns_record_fails_rather_than_passing(monkeypatch, sender_ready):
    """A record REACH looked up and did not find is FAIL, not UNKNOWN."""
    from reach import dns_checks

    from .conftest import SENDER_DNS, SENDER_DOMAIN

    without_spf = {key: value for key, value in SENDER_DNS.items()
                   if key != (SENDER_DOMAIN, "TXT")}
    dns_checks.set_resolver(dns_checks.offline_resolver(without_spf))

    health = sender.health_summary()
    states = {check["key"]: check["state"] for check in health["checks"]}
    assert states["spf"] == sender.FAIL
    assert health["ready"] is False


def test_unresolvable_dns_reports_unknown_not_pass(monkeypatch, sender_ready):
    """A lookup REACH could not perform is UNKNOWN — never a silent PASS."""
    from reach import dns_checks

    monkeypatch.delenv("REACH_SENDER_SPF_VERIFIED", raising=False)
    dns_checks.set_resolver(lambda name, rdtype: dns_checks.Lookup(
        dns_checks.UNRESOLVED, name=name, rdtype=rdtype, detail="DNS timed out"))

    health = sender.health_summary()
    states = {check["key"]: check["state"] for check in health["checks"]}
    assert states["spf"] == sender.UNKNOWN
    assert states["dmarc"] == sender.UNKNOWN
    assert health["ready"] is False


def test_operator_declaration_covers_only_an_unresolvable_lookup(monkeypatch, sender_ready):
    """The declared-value escape hatch applies when DNS is unreachable, and it
    says so in the detail rather than implying REACH verified the record."""
    from reach import dns_checks

    dns_checks.set_resolver(lambda name, rdtype: dns_checks.Lookup(
        dns_checks.UNRESOLVED, name=name, rdtype=rdtype, detail="DNS unavailable"))
    monkeypatch.setenv("REACH_SENDER_SPF_VERIFIED", "v=spf1 -all")

    health = sender.health_summary()
    spf = next(check for check in health["checks"] if check["key"] == "spf")
    assert spf["state"] == sender.PASS
    assert "declared, not checked by REACH" in spf["detail"]


def test_verified_records_quote_what_was_actually_read(sender_ready):
    health = sender.health_summary()
    by_key = {check["key"]: check for check in health["checks"]}
    assert by_key["spf"]["state"] == sender.PASS
    assert "Verified by DNS" in by_key["spf"]["detail"]
    assert "v=spf1" in by_key["spf"]["detail"]
    assert by_key["dmarc_alignment"]["state"] == sender.PASS
    assert "p=reject" in by_key["dmarc_alignment"]["detail"]


def _health_with_spf(record, monkeypatch=None):
    from reach import dns_checks

    from .conftest import SENDER_DNS, SENDER_DOMAIN

    table = dict(SENDER_DNS)
    table[(SENDER_DOMAIN, "TXT")] = record if isinstance(record, list) else [record]
    dns_checks.set_resolver(dns_checks.offline_resolver(table))
    return sender.health_summary()


def test_a_permissive_spf_record_fails_the_gate(sender_ready):
    """+all is a published record that authorises every sender on the internet.

    Passing it because the lookup succeeded is the exact fake-green this gate
    exists to refuse.
    """
    health = _health_with_spf("v=spf1 include:provider.example +all")
    spf = next(c for c in health["checks"] if c["key"] == "spf")
    assert spf["state"] == sender.FAIL
    assert "no protection against forgery" in spf["detail"]
    assert health["ready"] is False


def test_an_spf_record_with_no_all_mechanism_fails_the_gate(sender_ready):
    health = _health_with_spf("v=spf1 include:provider.example")
    spf = next(c for c in health["checks"] if c["key"] == "spf")
    assert spf["state"] == sender.FAIL
    assert health["ready"] is False


def test_duplicate_spf_records_fail_the_gate(sender_ready):
    health = _health_with_spf(["v=spf1 -all", "v=spf1 +all"])
    assert next(c for c in health["checks"] if c["key"] == "spf")["state"] == sender.FAIL
    assert health["ready"] is False


def test_a_softfail_spf_record_still_passes(sender_ready):
    """~all is weaker than -all but genuinely challenges unlisted senders,
    and with DMARC enforcing it is a normal, correct configuration."""
    health = _health_with_spf("v=spf1 include:provider.example ~all")
    assert next(c for c in health["checks"] if c["key"] == "spf")["state"] == sender.PASS
    assert health["ready"] is True


def test_a_declaration_cannot_override_a_record_reach_actually_read(sender_ready, monkeypatch):
    """The operator override exists for "REACH could not look", not for
    "REACH looked and did not like what it found"."""
    monkeypatch.setenv("REACH_SENDER_SPF_VERIFIED", "1")
    health = _health_with_spf("v=spf1 +all")
    spf = next(c for c in health["checks"] if c["key"] == "spf")
    assert spf["state"] == sender.FAIL
    assert "declared" not in spf["detail"]
    assert health["ready"] is False


def test_a_revoked_dkim_key_fails_the_gate(sender_ready):
    from reach import dns_checks

    from .conftest import SENDER_DNS, SENDER_DOMAIN

    table = dict(SENDER_DNS)
    table[(f"selector1._domainkey.{SENDER_DOMAIN}", "TXT")] = ["v=DKIM1; k=rsa; p="]
    dns_checks.set_resolver(dns_checks.offline_resolver(table))
    health = sender.health_summary()
    dkim = next(c for c in health["checks"] if c["key"] == "dkim")
    assert dkim["state"] == sender.FAIL
    assert "revoked" in dkim["detail"]
    assert health["ready"] is False


def test_dmarc_p_none_is_not_treated_as_enforcement(sender_ready):
    """p=none monitors but does not enforce, and must not read as alignment."""
    from reach import dns_checks

    from .conftest import SENDER_DNS, SENDER_DOMAIN

    monitoring_only = dict(SENDER_DNS)
    monitoring_only[(f"_dmarc.{SENDER_DOMAIN}", "TXT")] = ["v=DMARC1; p=none"]
    dns_checks.set_resolver(dns_checks.offline_resolver(monitoring_only))

    health = sender.health_summary()
    by_key = {check["key"]: check for check in health["checks"]}
    assert by_key["dmarc"]["state"] == sender.PASS       # it is published
    assert by_key["dmarc_alignment"]["state"] == sender.UNKNOWN  # but not enforcing
    assert "does not enforce" in by_key["dmarc_alignment"]["detail"]


def test_drafts_only_when_sender_health_fails(scouted_without_sender, mail):
    target = campaigns.targets(scouted_without_sender)[0]
    decision = compliance_decision_for(target["id"])
    assert decision in ("DRAFT_ONLY", "BLOCK")
    assert mail.sent == []


def compliance_decision_for(target_id):
    from reach import compliance

    row = compliance.latest_decision(target_id)
    return row["decision"] if row else None
