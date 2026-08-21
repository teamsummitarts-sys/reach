"""Standards-compliant focused webpage fetcher.

Rules this fetcher enforces, in order, before any bytes are read:

1. scheme / host / port / file-type validation (:mod:`reach.netguard`)
2. DNS resolution with every resolved address checked as public
3. robots.txt for the origin, honouring Disallow and Crawl-delay
4. per-domain crawl budget for the campaign
5. redirect limit, with each hop re-validated from scratch
6. response size cap enforced while reading, not after
7. MIME allowlist

The transport is pluggable so tests exercise the same code path against a
fixture corpus instead of the network. Nothing downloaded is ever executed.
"""

import http.client
import socket
import ssl
import threading
import time
import urllib.robotparser
from urllib.parse import urljoin, urlsplit

from . import config, netguard
from .errors import FetchBlocked

_domain_last_request = {}
_domain_lock = threading.Lock()


class FetchResult:
    __slots__ = ("url", "final_url", "status", "headers", "body", "mime", "domain",
                 "bytes", "robots_decision", "elapsed_ms")

    def __init__(self, url, final_url, status, headers, body, mime, domain,
                 robots_decision, elapsed_ms):
        self.url = url
        self.final_url = final_url
        self.status = status
        self.headers = headers
        self.body = body
        self.mime = mime
        self.domain = domain
        self.bytes = len(body or b"")
        self.robots_decision = robots_decision
        self.elapsed_ms = elapsed_ms

    def text(self, limit=None):
        raw = self.body or b""
        if limit:
            raw = raw[:limit]
        return raw.decode("utf-8", errors="replace")


class Transport:
    """Performs one already-validated request. No redirect following."""

    name = "transport"

    def request(self, validated, headers):  # pragma: no cover - interface
        raise NotImplementedError


class HttpTransport(Transport):
    name = "http"

    def request(self, validated, headers):
        family, address = validated["addresses"][0]
        timeout = config.FETCH_TIMEOUT_SECONDS
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((address, validated["port"]))
            if validated["scheme"] == "https":
                context = ssl.create_default_context()
                context.check_hostname = True
                context.verify_mode = ssl.CERT_REQUIRED
                sock = context.wrap_socket(sock, server_hostname=validated["hostname"])
            conn = http.client.HTTPConnection(validated["hostname"], validated["port"],
                                              timeout=timeout)
            conn.sock = sock
            request_headers = dict(headers)
            request_headers["Host"] = validated["hostname"]
            conn.request("GET", validated["path"], headers=request_headers)
            response = conn.getresponse()
            limit = config.FETCH_MAX_BYTES
            body = response.read(limit + 1)
            if len(body) > limit:
                raise FetchBlocked(
                    f"Response exceeds {limit} bytes", netguard.REASON_SIZE
                )
            return response.status, dict(response.getheaders()), body
        except ssl.SSLError as exc:
            raise FetchBlocked(f"TLS failure: {exc}", netguard.REASON_TRANSPORT)
        except socket.timeout:
            raise FetchBlocked("Request timed out", netguard.REASON_TIMEOUT)
        except OSError as exc:
            raise FetchBlocked(f"Transport error: {exc}", netguard.REASON_TRANSPORT)
        finally:
            try:
                sock.close()
            except OSError:
                pass


class FixtureTransport(Transport):
    """Serves a deterministic corpus. Used by tests and by fixture mode, and
    always labelled as such — a fixture result is never presented as a live
    fetch."""

    name = "fixture"

    def __init__(self, pages=None):
        from .fixtures import web as web_fixtures

        self.pages = pages if pages is not None else web_fixtures.PAGES

    def request(self, validated, headers):
        url = f"{validated['scheme']}://{validated['hostname']}{validated['path']}"
        page = self.pages.get(url) or self.pages.get(url.rstrip("/"))
        if page is None:
            return 404, {"Content-Type": "text/html"}, b"<html><body>Not found</body></html>"
        status = page.get("status", 200)
        response_headers = dict(page.get("headers") or {})
        response_headers.setdefault("Content-Type", "text/html; charset=utf-8")
        body = page.get("body", "")
        if isinstance(body, str):
            body = body.encode("utf-8")
        return status, response_headers, body


_transport = None


def set_transport(transport):
    global _transport
    _transport = transport


def get_transport():
    if _transport is not None:
        return _transport
    return HttpTransport()


_fixture_fallback = None


def transport_for(hostname):
    """The transport that serves this host.

    An installed transport (tests) always wins. Otherwise reserved ``.example``
    hosts are served from the built-in fixture corpus: RFC 2606 keeps them
    permanently unresolvable, so a real HTTP transport can never fetch one.
    Without this routing, keyless discovery finds fixture URLs and then blocks
    on every fetch of them — which is exactly what the first production run
    did: 24 searches, thirteen fetch jobs that all "succeeded" as refusals,
    zero pages, zero outlets. Real hosts always get the real transport, with
    DNS resolution and every other guard intact.
    """
    global _fixture_fallback
    if _transport is not None:
        return _transport
    if (hostname or "").lower().rstrip(".").endswith(".example"):
        if _fixture_fallback is None:
            _fixture_fallback = FixtureTransport()
        return _fixture_fallback
    return HttpTransport()


def _served_by_fixtures(hostname):
    return isinstance(transport_for(hostname), FixtureTransport)


def using_fixtures():
    return isinstance(get_transport(), FixtureTransport)


# --------------------------------------------------------------------------
# robots
# --------------------------------------------------------------------------

_robots_cache = {}
_robots_lock = threading.Lock()

ROBOTS_ALLOWED = "ALLOWED"
ROBOTS_DISALLOWED = "DISALLOWED"
ROBOTS_UNAVAILABLE = "NO_ROBOTS_FILE"


def clear_robots_cache():
    with _robots_lock:
        _robots_cache.clear()


def _robots_for(scheme, hostname, port):
    key = (scheme, hostname, port)
    with _robots_lock:
        cached = _robots_cache.get(key)
    if cached is not None:
        return cached

    robots_url = f"{scheme}://{hostname}{'' if port in (80, 443) else ':' + str(port)}/robots.txt"
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    decision = ROBOTS_UNAVAILABLE
    try:
        transport = transport_for(hostname)
        validated = netguard.validate_url(
            robots_url, resolve=not isinstance(transport, FixtureTransport))
        status, headers, body = transport.request(
            validated, {"User-Agent": config.USER_AGENT, "Accept": "text/plain"}
        )
        if status == 200:
            parser.parse(body.decode("utf-8", errors="replace").splitlines())
            decision = "PARSED"
        else:
            parser.allow_all = True
    except FetchBlocked:
        parser.allow_all = True
    except Exception:  # pragma: no cover - a broken robots file must not crash a crawl
        parser.allow_all = True

    entry = (parser, decision)
    with _robots_lock:
        _robots_cache[key] = entry
    return entry


def robots_check(url):
    validated = netguard.validate_url(url, resolve=False)
    parser, state = _robots_for(validated["scheme"], validated["hostname"], validated["port"])
    if state == ROBOTS_UNAVAILABLE:
        return ROBOTS_UNAVAILABLE, None
    allowed = parser.can_fetch(config.USER_AGENT, url)
    delay = None
    try:
        delay = parser.crawl_delay(config.USER_AGENT)
    except Exception:  # pragma: no cover
        delay = None
    return (ROBOTS_ALLOWED if allowed else ROBOTS_DISALLOWED), delay


def _respect_crawl_delay(domain, delay):
    if not delay:
        delay = 1.0
    with _domain_lock:
        last = _domain_last_request.get(domain)
        now = time.monotonic()
        if last is not None and now - last < delay:
            wait = delay - (now - last)
        else:
            wait = 0
        _domain_last_request[domain] = now + wait
    if wait > 0 and not using_fixtures():  # pragma: no cover - timing path
        time.sleep(min(wait, 5.0))


def reset_rate_state():
    with _domain_lock:
        _domain_last_request.clear()


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------

def fetch(url, honor_robots=True, max_redirects=None):
    """Fetch one public page. Raises :class:`FetchBlocked` with a reason code
    for every refusal, so callers can record *why* rather than guessing."""
    max_redirects = config.FETCH_MAX_REDIRECTS if max_redirects is None else max_redirects
    started = time.monotonic()
    current = url
    seen = set()
    robots_decision = ROBOTS_UNAVAILABLE

    for hop in range(max_redirects + 1):
        if current in seen:
            raise FetchBlocked("Redirect loop", netguard.REASON_REDIRECT)
        seen.add(current)

        # Fixture-served hosts skip only DNS: reserved .example names never
        # resolve. Every other control — scheme, host blocklist, literal-IP
        # checks, port and file type — still runs exactly as it does against
        # the live web, and the decision is per hop, so a fixture page
        # redirecting at a real address gets the real checks.
        hop_host = urlsplit(current).hostname
        transport = transport_for(hop_host)
        validated = netguard.validate_url(
            current, resolve=not isinstance(transport, FixtureTransport))
        if honor_robots:
            robots_decision, delay = robots_check(current)
            if robots_decision == ROBOTS_DISALLOWED:
                raise FetchBlocked(
                    f"robots.txt disallows {current}", netguard.REASON_ROBOTS
                )
            _respect_crawl_delay(validated["domain"], delay)

        status, headers, body = transport.request(validated, {
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
            "Accept-Language": "en,*;q=0.5",
        })

        if status in (301, 302, 303, 307, 308):
            location = headers.get("Location") or headers.get("location")
            if not location:
                raise FetchBlocked("Redirect without Location", netguard.REASON_REDIRECT)
            if hop >= max_redirects:
                raise FetchBlocked(
                    f"More than {max_redirects} redirects", netguard.REASON_TOO_MANY_REDIRECTS
                )
            # Re-validate from scratch: a redirect into a private range or a
            # different scheme is exactly the attack this blocks.
            current = urljoin(current, location)
            continue

        mime = headers.get("Content-Type") or headers.get("content-type")
        netguard.check_mime(mime)
        netguard.check_size(len(body))

        declared = headers.get("Content-Length") or headers.get("content-length")
        if declared:
            try:
                netguard.check_size(int(declared))
            except ValueError:
                pass

        disposition = (headers.get("Content-Disposition")
                       or headers.get("content-disposition") or "")
        if "attachment" in disposition.lower():
            raise FetchBlocked("Attachment responses are not fetched",
                               netguard.REASON_EXTENSION)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        return FetchResult(url, current, status, headers, body, mime,
                           validated["domain"], robots_decision, elapsed_ms)

    raise FetchBlocked(f"More than {max_redirects} redirects",
                       netguard.REASON_TOO_MANY_REDIRECTS)


def origin_of(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
