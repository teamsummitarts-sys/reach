"""What the first production deploy taught us, pinned as tests.

Every other test installs :class:`fetcher.FixtureTransport`, which is exactly
why the deploy failed in a way no test caught: in production nothing installs
a transport, keyless search returns reserved ``.example`` URLs, and the real
HTTP transport can never fetch one — RFC 2606 keeps those names permanently
unresolvable. The result was a discovery run where 24 searches succeeded,
every fetch job "succeeded" as a polite refusal, and nothing was discovered.

These tests run with **no transport installed**, the way production actually
runs. The contract: fixture-corpus hosts are served from the corpus, real
hosts keep the real transport with every guard intact, and a keyless
end-to-end discovery run actually discovers.
"""

import pytest

from reach import campaigns, fetcher, pipeline
from reach.errors import FetchBlocked


@pytest.fixture
def production_transport():
    """No installed transport — the deployed configuration."""
    fetcher.set_transport(None)
    fetcher.clear_robots_cache()
    yield
    fetcher.set_transport(fetcher.FixtureTransport())
    fetcher.clear_robots_cache()


def test_fixture_hosts_are_served_from_the_corpus(production_transport):
    result = fetcher.fetch("https://bassforge.example/promo")
    assert result.status == 200
    assert "Bassforge" in result.text()


def test_real_hosts_keep_the_real_transport(production_transport):
    """Only .example routes to fixtures; everything else is a real fetch."""
    assert isinstance(fetcher.transport_for("bassforge.example"), fetcher.FixtureTransport)
    assert isinstance(fetcher.transport_for("example.com"), fetcher.HttpTransport)
    assert isinstance(fetcher.transport_for("notexample.com"), fetcher.HttpTransport)
    # A crafted name that merely contains the string is not a fixture host.
    assert isinstance(fetcher.transport_for("example.evil.com"), fetcher.HttpTransport)


def test_an_installed_transport_still_wins(production_transport):
    """Tests that install a recording transport must keep owning every host."""
    recorder = fetcher.FixtureTransport()
    fetcher.set_transport(recorder)
    assert fetcher.transport_for("anything.com") is recorder


def test_fixture_serving_does_not_relax_the_other_guards(production_transport):
    """DNS is the only skipped check. The redirect out of redirector.example
    lands on a cloud metadata address and must still be blocked."""
    with pytest.raises(FetchBlocked):
        fetcher.fetch("https://redirector.example/go")


def test_robots_are_honoured_for_fixture_served_hosts(production_transport):
    with pytest.raises(FetchBlocked):
        fetcher.fetch("https://norobots.example/private/contact")


def test_keyless_discovery_discovers_with_no_transport_installed(
        production_transport, campaign_id):
    """The end-to-end production scenario: no search key, no transport.

    This is the test that would have caught the deploy failure. It asserts the
    outcome that was missing in production — pages actually fetched, outlets
    actually discovered — not merely that jobs completed.
    """
    pipeline.start(campaign_id)
    pipeline.run_to_completion(campaign_id)

    spent = campaigns.budget(campaign_id)
    assert spent["pages_fetched"] > 0, \
        "every fetch was refused — fixture URLs are not reaching the fixture corpus"
    assert len(campaigns.targets(campaign_id)) > 0, "nothing was discovered"
