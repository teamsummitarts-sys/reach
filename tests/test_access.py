"""The access gate.

REACH gained a public landing page, which means the URL gets shared — and the
app behind it is a single artist's private workspace. When REACH_ACCESS_KEY is
set, every /reach screen and action requires the key once per browser. When it
is not set, the gate does not exist and nothing changes.
"""

import pytest

from app import create_app

KEY = "open-sesame-9000"


@pytest.fixture
def open_client():
    return create_app().test_client()


@pytest.fixture
def locked_client(monkeypatch):
    monkeypatch.setenv("REACH_ACCESS_KEY", KEY)
    return create_app().test_client()


def test_without_the_variable_there_is_no_gate(open_client):
    assert open_client.get("/reach/").status_code == 200
    # And the unlock page has nothing to do, so it forwards into the app.
    response = open_client.get("/reach/unlock")
    assert response.status_code == 302


def test_locked_pages_redirect_to_the_unlock_form(locked_client):
    response = locked_client.get("/reach/campaigns")
    assert response.status_code == 302
    assert "/reach/unlock" in response.headers["Location"]
    assert "next=" in response.headers["Location"]


def test_locked_actions_refuse_with_json_not_a_redirect(locked_client):
    response = locked_client.post("/reach/catalog", json={"title": "X", "artist_name": "Y"})
    assert response.status_code == 401
    assert response.get_json()["kind"] == "LOCKED"


def test_the_wrong_key_does_not_unlock(locked_client):
    body = locked_client.post("/reach/unlock", data={"key": "guess", "next": "/reach/"},
                              follow_redirects=True).get_data(as_text=True)
    assert "That key is not correct." in body
    assert locked_client.get("/reach/").status_code == 302


def test_the_right_key_unlocks_this_browser(locked_client):
    response = locked_client.post("/reach/unlock", data={"key": KEY, "next": "/reach/campaigns"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/reach/campaigns")
    assert locked_client.get("/reach/").status_code == 200
    assert locked_client.post("/reach/catalog",
                              json={"title": "Gate Test", "artist_name": "A"}
                              ).get_json()["ok"] is True


def test_the_unlock_redirect_never_leaves_the_site(locked_client):
    response = locked_client.post("/reach/unlock",
                                  data={"key": KEY, "next": "//evil.example/phish"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/reach/")


def test_the_webhook_keeps_its_own_authentication(locked_client):
    """The email provider cannot enter an access key. The webhook stays
    reachable and keeps refusing unsigned requests on its own terms."""
    response = locked_client.post("/reach/webhooks/email",
                                  json={"type": "bounce", "email": "x@example.test"})
    assert response.status_code == 401
    payload = response.get_json() or {}
    assert payload.get("kind") != "LOCKED"


def test_the_front_door_stays_public(locked_client):
    assert locked_client.get("/").status_code == 200
    assert locked_client.get("/healthz").status_code == 200
