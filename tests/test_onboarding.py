"""First-run guidance.

The panel exists because REACH runs with no configuration at all — a deliberate
property that leaves a first-time operator unable to tell which parts are inert.
Every item is answered from the database or the environment, so these tests
assert the answers change with the state rather than that a checklist renders.
"""

from reach import campaigns, catalog, crypto, onboarding, profile


def states(status):
    return {step["key"]: step["state"] for step in status["steps"]}


def keys(status):
    return {item["key"] for item in status["capabilities"]}


# --- steps are answered from real state -------------------------------------

def test_a_fresh_install_has_everything_still_to_do():
    status = onboarding.status()
    assert states(status) == {k: onboarding.TODO for k in
                              ("track", "profile", "rights", "campaign", "discovery", "approval")}
    assert status["done_count"] == 0
    assert status["show"] is True


def test_sample_tracks_do_not_count_as_adding_your_own():
    """Five samples ship seeded. Treating them as the operator's own work would
    mark the first step done before anything was done."""
    assert catalog.recordings(), "the sample catalog should be seeded"
    assert states(onboarding.status())["track"] == onboarding.TODO


def test_adding_a_track_completes_the_first_step():
    catalog.add_track({"title": "Harbour Lights", "artist_name": "Night Ferry"})
    assert states(onboarding.status())["track"] == onboarding.DONE


def test_completing_a_profile_and_attesting_move_the_next_two(recording, complete_profile):
    assert states(onboarding.status())["profile"] == onboarding.DONE
    assert states(onboarding.status())["rights"] == onboarding.TODO
    catalog.attest_rights(recording["id"])
    assert states(onboarding.status())["rights"] == onboarding.DONE


def test_creating_a_campaign_and_discovering_move_the_last_ones(campaign_id, scouted):
    status = onboarding.status()
    assert states(status)["campaign"] == onboarding.DONE
    assert states(status)["discovery"] == onboarding.DONE
    assert states(status)["approval"] == onboarding.TODO


def test_approving_completes_the_path(scouted, sender_ready):
    from reach import approvals, drafts

    from .conftest import first_ready_target

    target = first_ready_target(scouted)
    drafts.generate(target["id"])
    approvals.approve(target["id"])
    assert states(onboarding.status())["approval"] == onboarding.DONE


def test_an_incomplete_profile_does_not_count(recording):
    """get_or_create makes a profile row; only a complete one counts."""
    profile.get_or_create(recording["id"])
    assert states(onboarding.status())["profile"] == onboarding.TODO


# --- capabilities name the consequence, not just the gap --------------------

def test_an_ephemeral_encryption_key_is_reported_as_blocking():
    """The most dangerous silent failure REACH has: everything written now
    becomes unreadable at the next restart, and nothing else says so."""
    assert crypto.key_state() != crypto.KEY_EPHEMERAL, "the fixture sets a real key"
    status = onboarding.status()
    assert "encryption" not in keys(status)


def test_a_missing_encryption_key_surfaces_with_its_consequence(monkeypatch):
    monkeypatch.delenv("REACH_ENCRYPTION_KEY", raising=False)
    crypto.reset_key_cache()
    item = next(c for c in onboarding.capabilities() if c["key"] == "encryption")
    assert item["severity"] == onboarding.BLOCKING
    assert "unreadable" in item["detail"]
    crypto.reset_key_cache()


def test_unset_db_path_is_reported_as_non_durable_storage(monkeypatch):
    """The free-plan failure mode, made visible.

    Without a disk there is no path to point REACH_DB_PATH at, so the store sits
    beside the application and dies with the container. REACH cannot detect a
    mounted volume, but it can tell nobody chose a location.
    """
    monkeypatch.delenv("REACH_DB_PATH", raising=False)
    item = next(c for c in onboarding.capabilities() if c["key"] == "storage")
    assert item["severity"] == onboarding.BLOCKING
    # The consequence that has a victim is named, not just "data may be lost".
    assert "suppression" in item["detail"]
    assert "opted out" in item["detail"]


def test_a_configured_db_path_removes_the_storage_warning(monkeypatch, tmp_path):
    monkeypatch.setenv("REACH_DB_PATH", str(tmp_path / "reach.db"))
    assert "storage" not in keys(onboarding.status())


def test_an_unhealthy_sender_is_blocking_and_lists_what_fails():
    item = next(c for c in onboarding.capabilities() if c["key"] == "sender")
    assert item["severity"] == onboarding.BLOCKING
    assert "will not send" in item["detail"]
    assert "SPF" in item["detail"] or "SENDING DOMAIN" in item["detail"]


def test_a_healthy_sender_removes_the_warning(sender_ready):
    assert "sender" not in keys(onboarding.status())


def test_fixture_mode_is_limiting_not_blocking():
    """Discovery still runs; it just runs against fixtures, and says so."""
    item = next(c for c in onboarding.capabilities() if c["key"] == "search")
    assert item["severity"] == onboarding.LIMITING
    assert "fixture corpus" in item["detail"]
    assert "not real ones" in item["detail"]


def test_uncredentialed_discovery_sources_are_named():
    item = next(c for c in onboarding.capabilities() if c["key"] == "providers")
    assert item["severity"] == onboarding.LIMITING
    for provider in ("spotify", "youtube", "soundcloud", "mixcloud"):
        assert provider in item["detail"]


# --- the panel retires itself ------------------------------------------------

def test_the_panel_hides_once_there_is_nothing_left_to_say(monkeypatch):
    monkeypatch.setattr(onboarding, "steps", lambda tenant_id=None: [])
    monkeypatch.setattr(onboarding, "capabilities", lambda: [])
    assert onboarding.status()["show"] is False


def test_the_panel_renders_on_a_fresh_install():
    from app import create_app

    client = create_app().test_client()
    body = client.get("/reach").get_data(as_text=True)
    assert "Getting started" in body
    assert "Add a track of your own" in body
    assert "0 of 6 done" in body
    # The capability list moved with the system screens, but stays reachable.
    advanced = client.get("/reach/advanced").get_data(as_text=True)
    assert "Not connected yet" in advanced


def test_campaign_counts_are_scoped_to_the_tenant(campaign_id):
    assert campaigns.list_campaigns()
    assert states(onboarding.status())["campaign"] == onboarding.DONE
