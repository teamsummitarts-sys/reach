"""Real DNS lookups for sender authentication.

Until now SPF, DKIM and DMARC were *operator-declared*: REACH could not query
TXT records with the standard library, so it read a variable saying "I checked
this myself". That is a weak link in a gate whose whole purpose is to refuse to
show green for something unverified.

This module performs the lookups. The three states it returns are deliberately
distinct:

* ``FOUND``      — the record exists and REACH read it
* ``ABSENT``     — the lookup succeeded and the record is genuinely not there
* ``UNRESOLVED`` — the lookup itself failed (no resolver, timeout, SERVFAIL)

``ABSENT`` and ``UNRESOLVED`` both fail the sender-health gate, but they are
never conflated: one means "you have not published this", the other means
"REACH could not find out", and the operator needs to tell them apart.
"""

import threading
import time

try:
    import dns.exception
    import dns.resolver
    DNS_AVAILABLE = True
except ImportError:  # pragma: no cover - dnspython is in requirements.txt
    DNS_AVAILABLE = False

FOUND = "FOUND"
ABSENT = "ABSENT"
UNRESOLVED = "UNRESOLVED"

TIMEOUT_SECONDS = 5.0
CACHE_TTL_SECONDS = 300

_cache = {}
_lock = threading.Lock()

# Tests and offline environments install their own resolver here rather than
# reaching the network. A resolver is a callable (name, rdtype) -> Lookup.
_resolver_override = None


def set_resolver(resolver):
    """Install a resolver. Pass None to restore real DNS."""
    global _resolver_override
    _resolver_override = resolver
    clear_cache()


def offline_resolver(records=None):
    """A resolver backed by a dict of {(name, rdtype): [values]}.

    Anything not in the dict resolves as ABSENT — the honest answer for "this
    name exists in DNS but publishes no such record".
    """
    table = {(name.lower(), rdtype): values for (name, rdtype), values in (records or {}).items()}

    def resolve(name, rdtype):
        values = table.get((name.lower(), rdtype))
        if values is None:
            return Lookup(ABSENT, name=name, rdtype=rdtype,
                          detail=f"No {rdtype} record published at {name}")
        return Lookup(FOUND, records=list(values), name=name, rdtype=rdtype,
                      detail=f"{len(values)} {rdtype} record(s) at {name}")

    return resolve


class Lookup:
    """One DNS answer, with enough context to render an honest detail line."""

    __slots__ = ("state", "records", "detail", "name", "rdtype")

    def __init__(self, state, records=None, detail="", name="", rdtype=""):
        self.state = state
        self.records = records or []
        self.detail = detail
        self.name = name
        self.rdtype = rdtype

    @property
    def found(self):
        return self.state == FOUND

    @property
    def first(self):
        return self.records[0] if self.records else None

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"Lookup({self.state}, {self.name} {self.rdtype}, {len(self.records)} records)"


def clear_cache():
    with _lock:
        _cache.clear()


def _cached(key):
    with _lock:
        entry = _cache.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.monotonic() > expires_at:
        with _lock:
            _cache.pop(key, None)
        return None
    return value


def _store(key, value):
    with _lock:
        _cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, value)
    return value


def query(name, rdtype):
    """Resolve one record set. Never raises — the failure is the answer."""
    if not name:
        return Lookup(UNRESOLVED, detail="No domain configured", name=name, rdtype=rdtype)
    if not DNS_AVAILABLE and _resolver_override is None:
        return Lookup(UNRESOLVED, name=name, rdtype=rdtype,
                      detail="dnspython is not installed, so REACH cannot verify this record")

    key = (name.lower(), rdtype)
    cached = _cached(key)
    if cached is not None:
        return cached

    if _resolver_override is not None:
        return _store(key, _resolver_override(name, rdtype))

    resolver = dns.resolver.Resolver()
    resolver.timeout = TIMEOUT_SECONDS
    resolver.lifetime = TIMEOUT_SECONDS

    try:
        answer = resolver.resolve(name, rdtype)
    except dns.resolver.NXDOMAIN:
        return _store(key, Lookup(ABSENT, name=name, rdtype=rdtype,
                                  detail=f"{name} does not exist"))
    except dns.resolver.NoAnswer:
        return _store(key, Lookup(ABSENT, name=name, rdtype=rdtype,
                                  detail=f"No {rdtype} record published at {name}"))
    except dns.exception.Timeout:
        return _store(key, Lookup(UNRESOLVED, name=name, rdtype=rdtype,
                                  detail=f"DNS timed out resolving {name}"))
    except Exception as exc:
        return _store(key, Lookup(UNRESOLVED, name=name, rdtype=rdtype,
                                  detail=f"DNS lookup failed for {name}: {exc}"))

    records = [_render(rdata, rdtype) for rdata in answer]
    return _store(key, Lookup(FOUND, records=records, name=name, rdtype=rdtype,
                              detail=f"{len(records)} {rdtype} record(s) at {name}"))


def _render(rdata, rdtype):
    if rdtype == "TXT":
        # A TXT record arrives as one or more chunks that must be concatenated.
        return "".join(
            part.decode("utf-8", errors="replace") if isinstance(part, bytes) else str(part)
            for part in rdata.strings
        )
    if rdtype == "MX":
        return f"{rdata.preference} {rdata.exchange.to_text()}"
    return rdata.to_text()


# --------------------------------------------------------------------------
# sender authentication records
# --------------------------------------------------------------------------

def spf(domain):
    """SPF lives in a TXT record at the domain itself, starting ``v=spf1``."""
    lookup = query(domain, "TXT")
    if not lookup.found:
        return lookup
    matches = [record for record in lookup.records
               if record.strip().lower().startswith("v=spf1")]
    if not matches:
        return Lookup(ABSENT, name=domain, rdtype="TXT",
                      detail=f"{domain} publishes TXT records but none is an SPF policy")
    return Lookup(FOUND, records=matches, name=domain, rdtype="TXT",
                  detail=matches[0][:160])


def dkim(domain, selector):
    """DKIM lives at ``<selector>._domainkey.<domain>``."""
    if not selector:
        return Lookup(UNRESOLVED, name=domain, rdtype="TXT",
                      detail="No DKIM selector configured — set REACH_SENDER_DKIM_SELECTOR")
    name = f"{selector}._domainkey.{domain}"
    lookup = query(name, "TXT")
    if not lookup.found:
        return lookup
    matches = [record for record in lookup.records if "p=" in record]
    if not matches:
        return Lookup(ABSENT, name=name, rdtype="TXT",
                      detail=f"{name} exists but publishes no DKIM public key")
    return Lookup(FOUND, records=matches, name=name, rdtype="TXT",
                  detail=f"DKIM key published at {name}")


def dmarc(domain):
    """DMARC lives at ``_dmarc.<domain>``, starting ``v=DMARC1``."""
    name = f"_dmarc.{domain}"
    lookup = query(name, "TXT")
    if not lookup.found:
        return lookup
    matches = [record for record in lookup.records
               if record.strip().lower().startswith("v=dmarc1")]
    if not matches:
        return Lookup(ABSENT, name=name, rdtype="TXT",
                      detail=f"{name} exists but publishes no DMARC policy")
    return Lookup(FOUND, records=matches, name=name, rdtype="TXT",
                  detail=matches[0][:160])


def dmarc_policy(domain):
    """The ``p=`` value, or None. ``p=none`` is published but not enforcing."""
    lookup = dmarc(domain)
    if not lookup.found:
        return None
    for part in lookup.first.split(";"):
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "p":
            return value.strip().lower()
    return None


def mx(domain):
    """Mail exchangers for a recipient domain. Used for non-intrusive contact
    validation — REACH never probes a mailbox with an SMTP callback."""
    lookup = query(domain, "MX")
    if lookup.found:
        return lookup
    if lookup.state == ABSENT:
        # RFC 5321: a domain with an A record but no MX still accepts mail.
        fallback = query(domain, "A")
        if fallback.found:
            return Lookup(FOUND, records=fallback.records, name=domain, rdtype="A",
                          detail=f"No MX record; {domain} has an A record and may "
                                 "still accept mail")
    return lookup


# --------------------------------------------------------------------------
# what the records actually say
# --------------------------------------------------------------------------

# Finding a record and reading it are different things. A domain publishing
# ``v=spf1 +all`` has an SPF record, and that record authorises the entire
# internet to send as it — which is weaker than publishing nothing, because it
# tells a receiver the forgery is legitimate. Reporting that as a pass because
# the lookup succeeded is exactly the kind of fake green this system exists to
# refuse, so the qualifier is parsed and judged.

SPF_LOOKUP_MECHANISMS = ("include", "a", "mx", "ptr", "exists")
SPF_MAX_LOOKUPS = 10  # RFC 7208 s4.6.4

FAIL_ALL = "-"
SOFTFAIL_ALL = "~"
NEUTRAL_ALL = "?"
PASS_ALL = "+"

_QUALIFIER_MEANING = {
    FAIL_ALL: "everything not listed is rejected",
    SOFTFAIL_ALL: "everything not listed is marked suspicious",
    NEUTRAL_ALL: "the record makes no assertion about any sender",
    PASS_ALL: "every sender on the internet is authorised",
}


class SpfPolicy:
    """What an SPF record says, not merely that one exists.

    ``protective`` is the question that matters: does this record cause a
    receiver to treat an unlisted sender as anything other than legitimate?
    """

    __slots__ = ("state", "record", "record_count", "all_qualifier", "redirect",
                 "lookup_count", "problems", "detail")

    def __init__(self, state, record=None, record_count=0, all_qualifier=None,
                 redirect=None, lookup_count=0, problems=None, detail=""):
        self.state = state
        self.record = record
        self.record_count = record_count
        self.all_qualifier = all_qualifier
        self.redirect = redirect
        self.lookup_count = lookup_count
        self.problems = problems or []
        self.detail = detail

    @property
    def found(self):
        return self.state == FOUND

    @property
    def protective(self):
        """A record that leaves unlisted senders unchallenged protects nothing."""
        if not self.found or self.problems:
            return False
        if self.redirect and self.all_qualifier is None:
            # redirect= hands the decision to another domain's policy, which is
            # a legitimate construction rather than an absent one.
            return True
        return self.all_qualifier in (FAIL_ALL, SOFTFAIL_ALL)


def _spf_terms(record):
    return [term for term in record.replace("\t", " ").split(" ") if term]


def analyse_spf(domain):
    """Read the SPF record and judge it. Never raises."""
    lookup = spf(domain)
    if not lookup.found:
        return SpfPolicy(lookup.state, detail=lookup.detail)

    problems = []
    if len(lookup.records) > 1:
        # RFC 7208 s4.5: more than one SPF record is a permerror, and a receiver
        # hitting one stops evaluating rather than picking a winner.
        problems.append(
            f"{domain} publishes {len(lookup.records)} SPF records. RFC 7208 makes that a "
            "permanent error, so receivers will not evaluate any of them. Publish exactly one.")

    record = lookup.first
    qualifier = None
    redirect = None
    lookups = 0

    for term in _spf_terms(record)[1:]:  # [0] is the v=spf1 version tag
        lower = term.lower()
        if lower.startswith("redirect="):
            redirect = term.partition("=")[2]
            lookups += 1
            continue
        if lower.startswith("exp="):
            continue
        head = lower[1:] if lower[:1] in "+-~?" else lower
        name = head.split(":", 1)[0].split("=", 1)[0]
        if name == "all":
            qualifier = lower[0] if lower[0] in "+-~?" else PASS_ALL
        elif name in SPF_LOOKUP_MECHANISMS:
            lookups += 1

    if qualifier in (NEUTRAL_ALL, PASS_ALL):
        problems.append(
            f'The record reads "{record}". It ends in {qualifier}all, so '
            f"{_QUALIFIER_MEANING[qualifier]} — this policy provides no protection against "
            "forgery. End it with -all, or ~all while you are still finding your senders.")
    elif qualifier is None and not redirect:
        problems.append(
            f'The record reads "{record}". It has no \'all\' mechanism and no redirect=, so '
            "RFC 7208 treats unlisted senders as neutral — the same as ?all. Add -all to the end.")

    if lookups > SPF_MAX_LOOKUPS:
        problems.append(
            f"The record uses {lookups} DNS-lookup mechanisms directly; RFC 7208 permits "
            f"{SPF_MAX_LOOKUPS} in total, so receivers will return a permanent error.")

    if problems:
        detail = problems[0]
    elif qualifier:
        detail = f'Verified by DNS: "{record}" — {_QUALIFIER_MEANING[qualifier]}'
    else:
        detail = f'Verified by DNS: "{record}" — delegated by redirect={redirect}'

    return SpfPolicy(FOUND, record=record, record_count=len(lookup.records),
                     all_qualifier=qualifier, redirect=redirect, lookup_count=lookups,
                     problems=problems, detail=detail)


def spf_lookup_note(policy):
    """How far REACH got with the RFC 7208 lookup budget, stated honestly.

    REACH counts only the mechanisms written in the record. It does not follow
    ``include:`` chains, so a record that looks within budget here can still
    exceed it once those are expanded — and saying so is more useful than
    implying a check that was not performed.
    """
    if not policy.found:
        return None
    return (f"{policy.lookup_count} of the {SPF_MAX_LOOKUPS} permitted DNS lookups are used by "
            "this record directly. REACH does not expand include: chains, so the real total "
            "may be higher.")


class DkimKey:
    """A DKIM selector record, and whether it carries a usable key."""

    __slots__ = ("state", "record", "revoked", "key_type", "problems", "detail")

    def __init__(self, state, record=None, revoked=False, key_type=None,
                 problems=None, detail=""):
        self.state = state
        self.record = record
        self.revoked = revoked
        self.key_type = key_type
        self.problems = problems or []
        self.detail = detail

    @property
    def found(self):
        return self.state == FOUND

    @property
    def usable(self):
        return self.found and not self.revoked and not self.problems


def analyse_dkim(domain, selector):
    """Read the DKIM selector record and judge it. Never raises."""
    lookup = dkim(domain, selector)
    if not lookup.found:
        return DkimKey(lookup.state, detail=lookup.detail)

    record = lookup.first
    tags = {}
    for part in record.split(";"):
        key, _, value = part.strip().partition("=")
        if key:
            tags[key.strip().lower()] = value.strip()

    problems = []
    public_key = tags.get("p")
    # RFC 6376 s3.6.1: an empty p= value means the key has been revoked. The
    # record still exists, which is why a presence check reads it as healthy.
    revoked = public_key == ""
    if revoked:
        problems.append(
            f"The key at {selector}._domainkey.{domain} is published with an empty p= value, "
            "which RFC 6376 defines as revoked. Signatures made with it will not verify. "
            "Rotate the selector with your email provider.")

    return DkimKey(FOUND, record=record, revoked=revoked, key_type=tags.get("k", "rsa"),
                   problems=problems,
                   detail=problems[0] if problems else
                   f"Verified by DNS: {tags.get('k', 'rsa')} key published at "
                   f"{selector}._domainkey.{domain}")
