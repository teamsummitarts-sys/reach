"""Shared fixtures for the REACH suite.

Every test runs against an isolated in-memory database, the fixture web corpus
and a recording email transport. No test touches the network.
"""

import os

import pytest

from reach import (campaigns, catalog, clock, crypto, db, dns_checks, fetcher, jobs,
                   pipeline, policy, profile, rbac)
from reach.providers import email as email_provider
from reach.providers import soundcloud, spotify

SENDER_ENV = {
    "REACH_SENDER_FROM": "reach@outreach.streetbanker.example",
    "REACH_SENDER_NAME": "Street Banker REACH",
    "REACH_SENDER_POSTAL_ADDRESS": "12 Example Street, Berlin, Germany",
    "REACH_SENDER_POSTAL": "12 Example Street, Berlin, Germany",
    "REACH_SENDER_COUNTRY": "DE",
    "REACH_EMAIL_API_KEY": "test-email-key",
    "REACH_EMAIL_WEBHOOK_SECRET": "test-webhook-secret",
    "REACH_SENDER_DKIM_SELECTOR": "selector1",
    "REACH_SENDER_DOMAIN_VERIFIED": "1",
    "REACH_ENCRYPTION_KEY": "reach-test-key-0123456789abcdef",
}


@pytest.fixture(autouse=True)
def reach_environment(monkeypatch, tmp_path):
    """Isolated store, deterministic transports, no ambient credentials."""
    for key in list(os.environ):
        if key.startswith("REACH_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("REACH_DB_PATH", str(tmp_path / "reach.db"))
    monkeypatch.setenv("REACH_ENCRYPTION_KEY", SENDER_ENV["REACH_ENCRYPTION_KEY"])

    crypto.reset_key_cache()
    db.configure(str(tmp_path / "reach.db"))
    db.reset_database()

    # No test touches real DNS. The default resolver answers ABSENT for
    # everything, so a test that expects a passing sender must say so.
    dns_checks.set_resolver(dns_checks.offline_resolver())

    fetcher.set_transport(fetcher.FixtureTransport())
    fetcher.clear_robots_cache()
    fetcher.reset_rate_state()
    transport = email_provider.RecordingTransport()
    email_provider.set_transport(transport)
    soundcloud.reset_token_cache()
    spotify.reset_token_cache()

    rbac.clear_principal()
    rbac.ensure_default_tenant()
    policy.seed_policies()
    catalog.ensure_catalog()

    yield

    clock.unfreeze()
    dns_checks.set_resolver(None)
    fetcher.set_transport(None)
    email_provider.set_transport(None)
    rbac.clear_principal()
    db.close_all()


@pytest.fixture
def mail():
    """The recording email transport installed for this test."""
    return email_provider.get_transport()


SENDER_DOMAIN = "outreach.streetbanker.example"

# The DNS records a correctly configured sending domain publishes.
SENDER_DNS = {
    (SENDER_DOMAIN, "TXT"): ["v=spf1 include:provider.example -all"],
    (f"selector1._domainkey.{SENDER_DOMAIN}", "TXT"): ["v=DKIM1; k=rsa; p=MIIBIjAN"],
    (f"_dmarc.{SENDER_DOMAIN}", "TXT"): ["v=DMARC1; p=reject; rua=mailto:dmarc@streetbanker.example"],
}


@pytest.fixture
def sender_ready(monkeypatch):
    """A sending identity that passes the gate: configuration *and* published
    DNS. Both halves are required — configuration alone must not pass."""
    for key, value in SENDER_ENV.items():
        monkeypatch.setenv(key, value)
    dns_checks.set_resolver(dns_checks.offline_resolver(SENDER_DNS))
    crypto.reset_key_cache()
    return SENDER_ENV


@pytest.fixture
def recording():
    return catalog.recording_by_slug("midnight-drive")


@pytest.fixture
def complete_profile(recording):
    """A track profile complete enough to launch a campaign."""
    profile_id = profile.get_or_create(recording["id"])
    for field, value in [
        ("primary_genre", "dark electronic"),
        ("microgenres", ["dark electronic", "industrial", "coldwave"]),
        ("mood", ["brooding", "nocturnal"]),
        ("language", "English"),
        ("comparable_artists", ["HEALTH", "Boy Harsher"]),
    ]:
        profile.set_field(profile_id, field, value)
    return profile_id


@pytest.fixture
def attested(recording, complete_profile):
    return catalog.attest_rights(recording["id"])


@pytest.fixture
def campaign_id(recording, attested):
    return campaigns.create(recording["id"], mode=campaigns.COPILOT, territories=["DE"])


@pytest.fixture
def scouted(sender_ready, campaign_id):
    """A discovery run completed with a healthy sender, so qualification can
    reach READY. Tests that need the drafts-only path use
    :func:`scouted_without_sender` instead."""
    pipeline.start(campaign_id)
    pipeline.run_to_completion(campaign_id)
    return campaign_id


@pytest.fixture
def scouted_without_sender(campaign_id):
    """A discovery run with no sending identity configured."""
    pipeline.start(campaign_id)
    pipeline.run_to_completion(campaign_id)
    return campaign_id


def first_ready_target(campaign_id):
    rows = campaigns.targets(campaign_id, status=campaigns.READY)
    return rows[0] if rows else None


def any_live_target(campaign_id):
    """Any target that survived deduplication."""
    for row in campaigns.targets(campaign_id):
        if row["status"] != campaigns.DUPLICATE:
            return row
    return None


def target_named(campaign_id, fragment):
    for row in campaigns.targets(campaign_id):
        if fragment.lower() in (row["outlet_name"] or "").lower():
            return row
        if fragment.lower() in (row["outlet_domain"] or "").lower():
            return row
    return None


def drain():
    return jobs.run_pending()
