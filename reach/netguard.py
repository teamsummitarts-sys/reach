"""Network egress guard: SSRF, redirect and DNS-rebinding defenses.

Every outbound HTTP request REACH makes goes through :func:`validate_url` and
then connects to a *pre-resolved, already-validated IP* with the original
hostname carried in the ``Host`` header and TLS SNI. That closes the DNS
rebinding window: the address we checked is the address we connect to.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

from . import config
from .errors import FetchBlocked

ALLOWED_SCHEMES = ("https", "http")

BLOCKED_HOST_SUFFIXES = (
    ".local", ".internal", ".localdomain", ".cluster.local", ".svc", ".onion",
)
BLOCKED_HOSTNAMES = {
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "metadata", "metadata.google.internal", "metadata.goog",
    "instance-data", "instance-data.ec2.internal",
}
# Well-known cloud metadata endpoints.
BLOCKED_ADDRESSES = {
    "169.254.169.254",   # AWS / GCP / Azure / DigitalOcean IMDS
    "169.254.170.2",     # ECS task metadata
    "100.100.100.200",   # Alibaba Cloud
    "192.0.0.192",       # Oracle Cloud
    "fd00:ec2::254",     # AWS IMDS over IPv6
}

ALLOWED_MIME_PREFIXES = ("text/html", "text/plain", "application/xhtml+xml",
                         "application/xml", "text/xml", "application/rss+xml",
                         "application/atom+xml", "application/json")

# Extensions we refuse outright — REACH never downloads an executable or an
# archive, and nothing fetched is ever executed.
BLOCKED_EXTENSIONS = (
    ".exe", ".dll", ".so", ".dylib", ".bat", ".cmd", ".com", ".msi", ".scr",
    ".sh", ".bash", ".ps1", ".jar", ".apk", ".deb", ".rpm", ".pkg", ".dmg",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".iso", ".img",
)

REASON_SCHEME = "BLOCKED_SCHEME"
REASON_HOSTNAME = "BLOCKED_HOSTNAME"
REASON_PRIVATE_IP = "BLOCKED_PRIVATE_IP"
REASON_METADATA = "BLOCKED_METADATA_ENDPOINT"
REASON_DNS = "DNS_RESOLUTION_FAILED"
REASON_REDIRECT = "BLOCKED_REDIRECT"
REASON_TOO_MANY_REDIRECTS = "TOO_MANY_REDIRECTS"
REASON_SIZE = "RESPONSE_TOO_LARGE"
REASON_MIME = "DISALLOWED_MIME_TYPE"
REASON_EXTENSION = "BLOCKED_FILE_TYPE"
REASON_ROBOTS = "ROBOTS_DISALLOWED"
REASON_PORT = "BLOCKED_PORT"
REASON_TIMEOUT = "FETCH_TIMEOUT"
REASON_TRANSPORT = "TRANSPORT_ERROR"

ALLOWED_PORTS = {80, 443, 8080, 8443}


def _is_blocked_address(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True, REASON_PRIVATE_IP
    if address in BLOCKED_ADDRESSES:
        return True, REASON_METADATA
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast:
        return True, REASON_PRIVATE_IP
    if ip.is_reserved or ip.is_unspecified:
        return True, REASON_PRIVATE_IP
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _is_blocked_address(str(ip.ipv4_mapped))
        if ip.is_site_local:
            return True, REASON_PRIVATE_IP
    return False, None


def resolve_host(hostname, port):
    """Resolve and validate every address the name maps to.

    All of them must be public: a name that resolves to one public and one
    private address is rejected, because we cannot control which one a later
    lookup would pick.
    """
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchBlocked(f"Could not resolve {hostname}: {exc}", REASON_DNS)
    if not infos:
        raise FetchBlocked(f"Could not resolve {hostname}", REASON_DNS)

    addresses = []
    for family, _type, _proto, _canon, sockaddr in infos:
        address = sockaddr[0]
        blocked, reason = _is_blocked_address(address)
        if blocked:
            raise FetchBlocked(
                f"{hostname} resolves to a blocked address ({address})", reason
            )
        addresses.append((family, address))
    return addresses


def validate_url(url, resolve=True):
    """Returns a normalized description of a URL REACH is allowed to fetch."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise FetchBlocked(f"Scheme {scheme or '(none)'} is not permitted", REASON_SCHEME)

    hostname = (parts.hostname or "").lower().rstrip(".")
    if not hostname:
        raise FetchBlocked("URL has no host", REASON_HOSTNAME)
    if hostname in BLOCKED_HOSTNAMES or hostname.endswith(BLOCKED_HOST_SUFFIXES):
        raise FetchBlocked(f"Host {hostname} is not permitted", REASON_HOSTNAME)

    # A literal IP in the URL is checked before any DNS work.
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        blocked, reason = _is_blocked_address(hostname)
        if blocked:
            raise FetchBlocked(f"Address {hostname} is not permitted", reason)

    port = parts.port or (443 if scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise FetchBlocked(f"Port {port} is not permitted", REASON_PORT)

    path = parts.path or "/"
    lowered = path.lower()
    if lowered.endswith(BLOCKED_EXTENSIONS):
        raise FetchBlocked(f"File type is not permitted: {path}", REASON_EXTENSION)

    addresses = resolve_host(hostname, port) if resolve else []
    return {
        "url": url,
        "scheme": scheme,
        "hostname": hostname,
        "port": port,
        "path": path + (("?" + parts.query) if parts.query else ""),
        "domain": registrable_domain(hostname),
        "addresses": addresses,
    }


def registrable_domain(hostname):
    """Good-enough eTLD+1 for per-domain budgets and dedup."""
    hostname = (hostname or "").lower().strip(".")
    if not hostname:
        return ""
    labels = hostname.split(".")
    if len(labels) <= 2:
        return hostname
    # Handles the common two-part public suffixes we actually meet (co.uk etc.)
    two_part_suffixes = {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au",
        "co.nz", "co.jp", "co.za", "com.br", "com.mx", "co.in",
    }
    tail = ".".join(labels[-2:])
    if tail in two_part_suffixes and len(labels) >= 3:
        return ".".join(labels[-3:])
    return tail


def check_mime(mime):
    if not mime:
        return
    base = mime.split(";")[0].strip().lower()
    if not base.startswith(ALLOWED_MIME_PREFIXES):
        raise FetchBlocked(f"MIME type {base} is not permitted", REASON_MIME)


def check_size(byte_count, limit=None):
    limit = limit or config.FETCH_MAX_BYTES
    if byte_count > limit:
        raise FetchBlocked(
            f"Response exceeds {limit} bytes (saw {byte_count})", REASON_SIZE
        )
