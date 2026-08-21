"""Contact, compliance, suppression and approval-integrity tests.

Covers Phase One scenarios 4–12, 27–30 and 34–37.
"""

import pytest

from reach import (approvals, audit, campaigns, compliance, contacts, crypto, drafts,
                   entities, evidence, jobs, outcomes, policy, rbac, scoring, sender)
from reach.errors import ApprovalInvalid, PermissionDenied, SendBlocked, ValidationError

from .conftest import first_ready_target, target_named


# --- 4–5: campaign modes ---------------------------------------------------

def test_scout_mode_never_sends(recording, attested, mail):
    campaign_id = campaigns.create(recording["id"], mode=campaigns.SCOUT,
                                   territories=["DE"])
    from reach import pipeline

    pipeline.start(campaign_id)
    pipeline.run_to_completion(campaign_id)

    assert mail.sent == []
    for target in campaigns.targets(campaign_id):
        decision = compliance.latest_decision(target["id"])
        if decision is not None:
            assert decision["decision"] != compliance.ALLOW


def test_scout_target_cannot_be_sent(recording, attested, sender_ready):
    from reach import pipeline

    campaign_id = campaigns.create(recording["id"], mode=campaigns.SCOUT,
                                   territories=["DE"])
    pipeline.start(campaign_id)
    pipeline.run_to_completion(campaign_id)
    targets = campaigns.targets(campaign_id)
    assert targets
    with pytest.raises((SendBlocked, ApprovalInvalid, ValidationError)):
        approvals.send_approved(targets[0]["id"])


def test_copilot_requires_an_approval_before_sending(scouted, sender_ready, mail):
    target = first_ready_target(scouted)
    assert target is not None
    drafts.generate(target["id"])
    with pytest.raises(ApprovalInvalid):
        approvals.send_approved(target["id"])
    assert mail.sent == []


def test_autopilot_cannot_be_created_in_phase_one(recording, attested):
    with pytest.raises(ValidationError) as info:
        campaigns.create(recording["id"], mode=campaigns.AUTOPILOT)
    assert "Autopilot is disabled" in str(info.value)


# --- 6: approval integrity -------------------------------------------------

def test_editing_a_draft_invalidates_its_approval(scouted, sender_ready):
    target = first_ready_target(scouted)
    draft_id = drafts.generate(target["id"])
    approvals.approve(target["id"])
    assert approvals.approval_is_valid(target["id"])[0] is True

    drafts.edit(draft_id, body="A completely different message body.")
    valid, reason = approvals.approval_is_valid(target["id"])
    assert valid is False
    assert "changed after approval" in reason


def test_send_refuses_an_invalidated_approval(scouted, sender_ready, mail):
    target = first_ready_target(scouted)
    draft_id = drafts.generate(target["id"])
    approvals.approve(target["id"])
    drafts.edit(draft_id, subject="Changed subject")

    with pytest.raises(ApprovalInvalid):
        approvals.send_approved(target["id"])
    assert mail.sent == []


def test_recipient_change_invalidates_the_payload_hash(scouted, sender_ready):
    target = first_ready_target(scouted)
    draft_id = drafts.generate(target["id"])
    draft = drafts.get(draft_id)

    original = drafts.payload_hash(draft["recipient_hash"], draft["subject"],
                                   draft["body"], [], [], 0.0, "reach@example.test")
    changed = drafts.payload_hash(crypto.hash_value("someone-else@example.test"),
                                  draft["subject"], draft["body"], [], [], 0.0,
                                  "reach@example.test")
    assert original != changed


def test_approved_send_delivers_exactly_the_approved_payload(scouted, sender_ready, mail):
    target = first_ready_target(scouted)
    draft_id = drafts.generate(target["id"])
    draft = drafts.get(draft_id)
    approvals.approve(target["id"])
    approvals.send_approved(target["id"])

    assert len(mail.sent) == 1
    message = mail.sent[0]
    assert message["subject"] == draft["subject"]
    assert message["text"] == draft["body"]
    assert message["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


# --- 7–8: Spotify-only contacts --------------------------------------------

def test_spotify_only_address_is_never_sendable():
    organization_id = contacts.ensure_organization("Spotify Curator", "spotify.com",
                                                   "CURATOR")
    contact_id = contacts.ensure_contact(organization_id)
    method_id = contacts.record_method(
        contact_id, "curator@example.test", contacts.SPOTIFY_ONLY_SOURCE,
        spotify_only=True,
    )
    state = contacts.sendability(method_id)
    assert state["sendable"] is False
    assert any("only through Spotify" in blocker for blocker in state["blockers"])


def test_spotify_only_contact_produces_a_block_decision(campaign_id):
    organization_id = contacts.ensure_organization("Spotify Curator", "spotify.com",
                                                   "CURATOR")
    contact_id = contacts.ensure_contact(organization_id)
    method_id = contacts.record_method(contact_id, "curator@example.test",
                                       contacts.SPOTIFY_ONLY_SOURCE, spotify_only=True)
    decision = compliance.decide(
        contact_method_id=method_id, campaign=campaigns.get(campaign_id),
    )
    assert decision["decision"] == compliance.BLOCK


def test_classification_marks_spotify_sourced_addresses():
    category, reasons = contacts.classify("music@example.test", provider="spotify")
    assert category == contacts.SPOTIFY_ONLY_SOURCE
    assert "independent professional source" in reasons[0]


def test_independently_verified_route_becomes_eligible():
    category, reasons = contacts.classify(
        "submissions@darkcircuit.example",
        page_classification="SUBMISSION_GUIDELINES",
        origin="mailto-link", role_based=True, page_is_official=True,
    )
    assert category == contacts.EXPLICIT_SUBMISSION_ROUTE
    assert category in contacts.SENDABLE_CATEGORIES


def test_personal_address_is_never_a_business_route():
    category, _ = contacts.classify("sam.personal@gmail.example",
                                    page_classification="BLOG")
    assert category == contacts.PERSONAL_ADDRESS
    assert category not in contacts.SENDABLE_CATEGORIES


def test_reach_refuses_to_guess_an_address_from_a_name():
    with pytest.raises(ValidationError):
        contacts.never_guess("firstname.lastname", "label.example")


# --- 9–11: suppression, bounces, opt-out -----------------------------------

def test_suppressed_contact_cannot_be_sent_to(scouted, sender_ready, mail):
    target = first_ready_target(scouted)
    drafts.generate(target["id"])
    approvals.approve(target["id"])

    method = approvals._method_for(target)
    contacts.suppress(contacts.reveal(method["id"]), "Manual test suppression")

    with pytest.raises(SendBlocked):
        approvals.send_approved(target["id"])
    assert mail.sent == []


def test_permanent_bounce_creates_suppression(scouted, sender_ready, mail):
    target = first_ready_target(scouted)
    drafts.generate(target["id"])
    approvals.approve(target["id"])
    approvals.send_approved(target["id"])

    method = approvals._method_for(target)
    address = contacts.reveal(method["id"])
    outcomes.record_response(target["id"], outcomes.BOUNCE)

    assert contacts.is_suppressed(address) is not None
    refreshed = campaigns.get_target(target["id"])
    assert refreshed["status"] == campaigns.BOUNCED


def test_opt_out_blocks_every_later_campaign(scouted, sender_ready, recording,
                                             attested, mail):
    target = first_ready_target(scouted)
    drafts.generate(target["id"])
    approvals.approve(target["id"])
    approvals.send_approved(target["id"])

    method = approvals._method_for(target)
    address = contacts.reveal(method["id"])
    outcomes.record_response(target["id"], outcomes.REPLY,
                             body_excerpt="Please unsubscribe me from this list.")

    assert campaigns.get_target(target["id"])["status"] == campaigns.OPTED_OUT
    assert contacts.is_suppressed(address) is not None

    # A brand-new campaign must still refuse this recipient.
    second = campaigns.create(recording["id"], mode=campaigns.COPILOT, name="Second run")
    decision = compliance.decide(contact_method_id=method["id"],
                                 campaign=campaigns.get(second))
    assert decision["decision"] == compliance.BLOCK


def test_follow_ups_are_cancelled_after_a_decline(scouted, sender_ready):
    from reach import db

    target = first_ready_target(scouted)
    drafts.generate(target["id"])
    approvals.approve(target["id"])
    approvals.send_approved(target["id"])

    pending = db.query("SELECT * FROM follow_up WHERE target_id = ? AND status = 'PENDING'",
                       (target["id"],))
    assert len(pending) == 1

    outcomes.record_response(target["id"], outcomes.DECLINE)
    pending = db.query("SELECT * FROM follow_up WHERE target_id = ? AND status = 'PENDING'",
                       (target["id"],))
    assert pending == []


# --- 12: consolidated duplicates -------------------------------------------

def test_one_curator_with_five_playlists_becomes_one_target(campaign_id, sender_ready):
    """Dark Circuit runs five playlists from a single submissions inbox.

    Fed deterministically rather than relying on which pages a search happens
    to rank, because the consolidation rule is what is under test.
    """
    import json

    from reach import jobs

    pages = [
        "https://darkcircuit.example/submit",
        "https://darkcircuit.example/playlists/void-transmission",
        "https://darkcircuit.example/playlists/iron-lung",
        "https://darkcircuit.example/playlists/night-shift",
        "https://darkcircuit.example/playlists/cold-static",
        "https://darkcircuit.example/playlists/black-mirrorball",
    ]
    for url in pages:
        jobs.enqueue("FETCH_PUBLIC_PAGE", {"url": url, "query": "test", "family": "PLAYLIST"},
                     idempotency_key=f"fetch:{campaign_id}:{url}", campaign_id=campaign_id)
    jobs.run_pending()

    rows = [row for row in campaigns.targets(campaign_id)
            if (row["outlet_domain"] or "") == "darkcircuit.example"]
    assert len(rows) == 6, "each page becomes its own outlet before deduplication"

    live = [row for row in rows if row["status"] != campaigns.DUPLICATE]
    assert len(live) == 1, "one inbox must produce exactly one contactable target"

    related = json.loads(live[0]["related_outlets_json"] or "[]")
    assert len(related) == 5, "the other five playlists hang off the survivor"


def test_duplicate_targets_never_generate_a_second_message(scouted, sender_ready, mail):
    rows = [row for row in campaigns.targets(scouted)
            if (row["outlet_domain"] or "") == "darkcircuit.example"]
    duplicates = [row for row in rows if row["status"] == campaigns.DUPLICATE]
    assert duplicates
    for duplicate in duplicates:
        with pytest.raises((SendBlocked, ApprovalInvalid, ValidationError)):
            approvals.send_approved(duplicate["id"])
    assert mail.sent == []


# --- 27–29: paid and guaranteed offers -------------------------------------

def test_guaranteed_streams_service_is_blocked(scouted):
    target = target_named(scouted, "streamboost")
    assert target is not None, "guaranteed-streams fixture should be discovered"
    assert target["status"] == campaigns.BLOCKED
    risk = scoring.latest_risk(target["id"])
    assert risk["band"] in (scoring.HIGH, scoring.BLOCK)


def test_guaranteed_placement_service_is_blocked(scouted):
    target = target_named(scouted, "playlistgold")
    assert target is not None
    assert target["status"] == campaigns.BLOCKED


def test_paid_consideration_requires_approval_and_is_not_placement(scouted):
    target = target_named(scouted, "curatorfee")
    assert target is not None
    route = entities.best_route(target["outlet_id"])
    assert route["cost_model"] == "PAID_CONSIDERATION"
    decision = compliance.latest_decision(target["id"])
    record = compliance.decision_record(decision)
    assert record["outreachCategory"] == compliance.PAID_CONSIDERATION
    assert any("never implies placement" in reason for reason in record["reasons"])


def test_paid_action_needs_the_spend_permission(scouted, sender_ready):
    target = target_named(scouted, "curatorfee")
    drafts.generate(target["id"])
    with rbac.as_role(rbac.REVIEWER):
        with pytest.raises(SendBlocked):
            approvals.approve(target["id"], cost=12.0)


# --- 30: tenant isolation --------------------------------------------------

def test_private_notes_are_not_visible_to_another_tenant():
    from reach import db, relationships

    organization_id = contacts.ensure_organization("Shared Outlet", "shared.example",
                                                   "BLOG")
    outlet_id = entities.ensure_outlet("Shared Outlet", "https://shared.example/",
                                       "shared.example", "BLOG")
    contact_id = contacts.ensure_contact(organization_id)
    relationship_id = relationships.touch(contact_id, outlet_id)
    relationships.add_note(relationship_id, "Private: prefers short pitches")

    assert len(relationships.notes(relationship_id)) == 1

    db.insert("tenant", {"id": "tenant_other", "name": "Other Artist",
                         "created_at": "2026-01-01T00:00:00+00:00"})
    assert relationships.notes(relationship_id, tenant_id="tenant_other") == []
    assert relationships.listing(tenant_id="tenant_other") == []


def test_suppression_is_global_across_tenants():
    contacts.suppress("shared-optout@example.test", "Opt-out")
    assert contacts.is_suppressed("shared-optout@example.test",
                                  tenant_id="tenant_other") is not None


# --- 34–35: kill switches ---------------------------------------------------

def test_emergency_send_stop_blocks_every_send(scouted, sender_ready, mail):
    target = first_ready_target(scouted)
    drafts.generate(target["id"])
    approvals.approve(target["id"])

    sender.emergency_stop(True, reason="test")
    with pytest.raises(SendBlocked):
        approvals.send_approved(target["id"])
    assert mail.sent == []

    sender.emergency_stop(False)
    approvals.send_approved(target["id"])
    assert len(mail.sent) == 1


def test_provider_kill_switch_stops_provider_calls():
    from reach.errors import KillSwitchEngaged
    from reach.providers import search as search_provider

    policy.set_kill_switch("open_web_search", True, reason="test")
    with pytest.raises(KillSwitchEngaged):
        search_provider.search("dark electronic playlist submissions")

    policy.set_kill_switch("open_web_search", False)
    assert search_provider.search("dark electronic playlist submissions") is not None


def test_global_stop_blocks_jobs(campaign_id):
    from reach import pipeline

    policy.set_global_stop(True, reason="test")
    pipeline.start(campaign_id)
    job = jobs.run_one()
    assert job["status"] in (jobs.PENDING, jobs.DEAD_LETTER, jobs.FAILED)
    assert job["error_kind"] == "KILL_SWITCH"
    policy.set_global_stop(False)


# --- 36–37: deletion and audit ---------------------------------------------

def test_provider_data_deletion_removes_stored_documents(scouted):
    from reach import db

    before = db.query_one(
        "SELECT COUNT(*) AS n FROM source_document WHERE provider = 'open_web_fetch'"
    )["n"]
    assert before > 0
    removed = evidence.delete_provider_data("open_web_fetch")
    assert removed == before
    after = db.query_one(
        "SELECT COUNT(*) AS n FROM source_document WHERE provider = 'open_web_fetch'"
    )["n"]
    assert after == 0


def test_audit_captures_external_actions_and_the_chain_verifies(scouted, sender_ready):
    target = first_ready_target(scouted)
    drafts.generate(target["id"])
    approvals.approve(target["id"])
    approvals.send_approved(target["id"])

    actions = {row["action"] for row in audit.events(limit=500)}
    for expected in ("campaign.created", "discovery.started", "draft.generated",
                     "outreach.approved", "outreach.sent", "compliance.decided"):
        assert expected in actions

    ok, broken = audit.verify_chain()
    assert ok is True and broken is None


def test_tampering_with_an_audit_row_breaks_the_chain(campaign_id):
    from reach import db

    rows = audit.events(limit=5)
    assert rows
    db.execute("UPDATE audit_event SET action = 'tampered' WHERE id = ?", (rows[0]["id"],))
    ok, broken = audit.verify_chain()
    assert ok is False
    assert broken is not None


# --- RBAC -------------------------------------------------------------------

def test_view_only_role_cannot_approve_or_send(scouted, sender_ready):
    target = first_ready_target(scouted)
    drafts.generate(target["id"])
    with rbac.as_role(rbac.VIEW_ONLY):
        with pytest.raises(PermissionDenied):
            approvals.approve(target["id"])
        with pytest.raises(PermissionDenied):
            approvals.send_approved(target["id"])


def test_reviewer_can_approve_but_not_send(scouted, sender_ready):
    target = first_ready_target(scouted)
    drafts.generate(target["id"])
    with rbac.as_role(rbac.REVIEWER):
        approvals.approve(target["id"])
        with pytest.raises(PermissionDenied):
            approvals.send_approved(target["id"])


def test_only_owner_may_enable_autopilot():
    assert rbac.PERMISSIONS["autopilot.enable"] == {rbac.OWNER}
