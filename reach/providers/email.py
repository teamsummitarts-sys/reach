"""Email delivery adapter.

Sending is a deterministic, tightly-permissioned action: the executor receives
an already-approved payload and delivers exactly that. This adapter does not
rewrite a message, change a recipient, or retry a permanent failure.

Without ``REACH_EMAIL_API_KEY`` the provider reports NOT CONNECTED and REACH is
drafts-only. Tests install :class:`RecordingTransport`, which records what would
have been sent without touching the network.
"""

import json
import threading
from urllib.parse import urlsplit

from .. import config, netguard, policy
from ..errors import ProviderNotConnected
from . import base

PROVIDER = "email"
DEFAULT_API_URL = "https://api.resend.com/emails"

_transport = None
_lock = threading.Lock()


class SendResult:
    __slots__ = ("accepted", "provider_message_id", "status_code", "error", "mode")

    def __init__(self, accepted, provider_message_id=None, status_code=None,
                 error=None, mode=base.LIVE):
        self.accepted = accepted
        self.provider_message_id = provider_message_id
        self.status_code = status_code
        self.error = error
        self.mode = mode


class RecordingTransport:
    """Captures messages instead of sending them. Never used in production."""

    mode = base.FIXTURE

    def __init__(self):
        self.sent = []
        self.fail_next = None

    def send(self, payload):
        if self.fail_next is not None:
            failure = self.fail_next
            self.fail_next = None
            return SendResult(False, error=failure, status_code=422, mode=base.FIXTURE)
        self.sent.append(payload)
        message_id = f"fixture-{len(self.sent):06d}"
        return SendResult(True, provider_message_id=message_id, status_code=202,
                          mode=base.FIXTURE)


class HttpTransport:
    mode = base.LIVE

    def send(self, payload):
        from ..fetcher import get_transport

        url = config.env("REACH_EMAIL_API_URL", DEFAULT_API_URL)
        validated = netguard.validate_url(url)
        body = json.dumps(payload).encode("utf-8")
        status_code, headers, raw = _post_json(get_transport(), validated, body)
        if status_code >= 400:
            return SendResult(False, status_code=status_code,
                              error=raw.decode("utf-8", errors="replace")[:300])
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            data = {}
        return SendResult(True, provider_message_id=data.get("id"), status_code=status_code)


def _post_json(transport, validated, body):
    import http.client
    import socket
    import ssl

    family, address = validated["addresses"][0]
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(config.FETCH_TIMEOUT_SECONDS)
    try:
        sock.connect((address, validated["port"]))
        if validated["scheme"] == "https":
            sock = ssl.create_default_context().wrap_socket(
                sock, server_hostname=validated["hostname"]
            )
        conn = http.client.HTTPConnection(validated["hostname"], validated["port"],
                                          timeout=config.FETCH_TIMEOUT_SECONDS)
        conn.sock = sock
        conn.request("POST", validated["path"], body=body, headers={
            "Host": validated["hostname"],
            "Authorization": f"Bearer {config.env('REACH_EMAIL_API_KEY')}",
            "Content-Type": "application/json",
            "User-Agent": config.USER_AGENT,
        })
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read(200_000)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def set_transport(transport):
    global _transport
    with _lock:
        _transport = transport


def get_transport():
    with _lock:
        if _transport is not None:
            return _transport
    return HttpTransport()


def using_recording_transport():
    return isinstance(get_transport(), RecordingTransport)


def connected():
    if using_recording_transport():
        return True
    return config.credentials_present(PROVIDER)


def status():
    state = base.state(PROVIDER)
    if using_recording_transport():
        state["state"] = policy.CONNECTED
        state["credentials_present"] = True
        state["mode"] = base.FIXTURE
    else:
        state["mode"] = base.LIVE
    return state


def send(to_address, subject, body_text, from_email, from_name, reply_to=None,
         headers=None, campaign_id=None):
    """Deliver one approved message. No mutation of any field is permitted."""
    policy.require_capability(PROVIDER, policy.AUTHENTICATED_WRITE)
    if not connected():
        raise ProviderNotConnected(
            "Email provider is NOT CONNECTED — missing REACH_EMAIL_API_KEY. "
            "REACH remains drafts-only."
        )
    payload = {
        "from": f"{from_name} <{from_email}>" if from_name else from_email,
        "to": [to_address],
        "subject": subject,
        "text": body_text,
        "headers": dict(headers or {}),
    }
    if reply_to:
        payload["reply_to"] = reply_to

    transport = get_transport()
    result = transport.send(payload)
    policy.record_request(
        PROVIDER, "send", "OK" if result.accepted else "ERROR",
        http_status=result.status_code, campaign_id=campaign_id,
        error=result.error,
    )
    return result


def sending_domain():
    from_email = config.env(config.SENDER_FROM_ENV)
    if from_email and "@" in from_email:
        return from_email.split("@", 1)[1].lower()
    return config.env(config.SENDER_DOMAIN_ENV)


def api_host():
    url = config.env("REACH_EMAIL_API_URL", DEFAULT_API_URL)
    return urlsplit(url).hostname
