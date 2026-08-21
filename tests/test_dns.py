"""DNS verification tests.

The distinction under test throughout: "we looked and it isn't there" (ABSENT)
must never be confused with "we couldn't look" (UNRESOLVED). One is the
operator's problem to fix; the other is REACH admitting it does not know.
"""

import pytest

from reach import contacts, dns_checks


@pytest.fixture
def records():
    table = {
        ("outlet.example", "TXT"): ["v=spf1 -all", "google-site-verification=abc"],
        ("sel._domainkey.outlet.example", "TXT"): ["v=DKIM1; k=rsa; p=KEY"],
        ("_dmarc.outlet.example", "TXT"): ["v=DMARC1; p=quarantine; pct=100"],
        ("outlet.example", "MX"): ["10 mail.outlet.example"],
        ("aonly.example", "A"): ["93.184.216.34"],
        ("plain.example", "TXT"): ["some-unrelated-value"],
    }
    dns_checks.set_resolver(dns_checks.offline_resolver(table))
    yield table
    dns_checks.set_resolver(None)


# --- the three states -------------------------------------------------------

def test_found_record_is_read_not_assumed(records):
    lookup = dns_checks.spf("outlet.example")
    assert lookup.state == dns_checks.FOUND
    assert lookup.first == "v=spf1 -all"
    assert lookup.found is True


def test_missing_record_is_absent_not_unresolved(records):
    assert dns_checks.spf("nothing.example").state == dns_checks.ABSENT
    assert dns_checks.dmarc("nothing.example").state == dns_checks.ABSENT


def test_failed_lookup_is_unresolved_not_absent():
    dns_checks.set_resolver(lambda name, rdtype: dns_checks.Lookup(
        dns_checks.UNRESOLVED, name=name, rdtype=rdtype, detail="timeout"))
    try:
        lookup = dns_checks.spf("outlet.example")
        assert lookup.state == dns_checks.UNRESOLVED
        assert lookup.found is False
    finally:
        dns_checks.set_resolver(None)


# --- record semantics -------------------------------------------------------

def test_txt_present_but_not_an_spf_policy_is_absent(records):
    """A domain with TXT records but no SPF has not published SPF."""
    lookup = dns_checks.spf("plain.example")
    assert lookup.state == dns_checks.ABSENT
    assert "none is an SPF policy" in lookup.detail


def test_spf_is_selected_from_among_other_txt_records(records):
    lookup = dns_checks.spf("outlet.example")
    assert lookup.records == ["v=spf1 -all"]
    assert "google-site-verification=abc" not in lookup.records


def test_dkim_requires_a_selector(records):
    lookup = dns_checks.dkim("outlet.example", None)
    assert lookup.state == dns_checks.UNRESOLVED
    assert "REACH_SENDER_DKIM_SELECTOR" in lookup.detail


def test_dkim_is_looked_up_under_the_selector(records):
    assert dns_checks.dkim("outlet.example", "sel").state == dns_checks.FOUND
    assert dns_checks.dkim("outlet.example", "wrong").state == dns_checks.ABSENT


def test_dmarc_policy_is_parsed(records):
    assert dns_checks.dmarc_policy("outlet.example") == "quarantine"
    assert dns_checks.dmarc_policy("nothing.example") is None


# --- MX ---------------------------------------------------------------------

def test_mx_record_is_found(records):
    lookup = dns_checks.mx("outlet.example")
    assert lookup.state == dns_checks.FOUND
    assert "mail.outlet.example" in lookup.first


def test_domain_with_only_an_a_record_may_still_accept_mail(records):
    """RFC 5321 implicit MX — reported as found, with the reason stated."""
    lookup = dns_checks.mx("aonly.example")
    assert lookup.state == dns_checks.FOUND
    assert "No MX record" in lookup.detail


def test_domain_with_no_mail_records_is_absent(records):
    assert dns_checks.mx("nothing.example").state == dns_checks.ABSENT


# --- caching ----------------------------------------------------------------

def test_lookups_are_cached_within_the_ttl(records):
    calls = []

    def counting(name, rdtype):
        calls.append((name, rdtype))
        return dns_checks.Lookup(dns_checks.FOUND, records=["v=spf1 -all"],
                                 name=name, rdtype=rdtype)

    dns_checks.set_resolver(counting)
    try:
        dns_checks.spf("cached.example")
        dns_checks.spf("cached.example")
        assert len(calls) == 1
    finally:
        dns_checks.set_resolver(None)


# --- contact validation -----------------------------------------------------

def test_contact_domain_check_uses_real_mail_records(records):
    organization_id = contacts.ensure_organization("Outlet", "outlet.example", "BLOG")
    contact_id = contacts.ensure_contact(organization_id)
    method_id = contacts.record_method(contact_id, "music@outlet.example",
                                       contacts.ROLE_BASED_ADDRESS, role_based=True)
    assert contacts.check_domain(method_id) is True

    row = contacts.get_method(method_id)
    assert row["mx_ok"] == 1
    assert row["mx_checked_at"] is not None


def test_contact_domain_check_reports_a_domain_that_cannot_receive_mail(records):
    organization_id = contacts.ensure_organization("Gone", "nothing.example", "BLOG")
    contact_id = contacts.ensure_contact(organization_id)
    method_id = contacts.record_method(contact_id, "music@nothing.example",
                                       contacts.ROLE_BASED_ADDRESS, role_based=True)
    assert contacts.check_domain(method_id) is False


def test_a_deliverable_domain_is_not_proof_of_a_mailbox(records):
    """The check is a weak signal by design — REACH never probes a mailbox."""
    import inspect

    source = inspect.getsource(contacts._has_mx)
    assert "never probes a mailbox" in source
    assert "SMTP" in source


# --- what the record actually says ------------------------------------------
#
# Finding a record and reading it are different things. Every case below used to
# report PASS with the sender ready to send, because the checks stopped at
# "a record exists". These assert the difference.

def spf_policy(record):
    domain = "sender.example"
    dns_checks.set_resolver(dns_checks.offline_resolver({
        (domain, "TXT"): record if isinstance(record, list) else [record]}))
    return dns_checks.analyse_spf(domain)


@pytest.mark.parametrize("record", [
    "v=spf1 include:provider.example -all",
    "v=spf1 include:provider.example ~all",
    "v=spf1 -all",
])
def test_a_protective_spf_policy_passes(record):
    policy = spf_policy(record)
    assert policy.found
    assert policy.protective
    assert policy.problems == []
    # It still quotes what was read, so the operator can check it themselves.
    assert record in policy.detail


@pytest.mark.parametrize("record,expected", [
    ("v=spf1 include:provider.example +all", "+"),
    ("v=spf1 +all", "+"),
    ("v=spf1 include:provider.example ?all", "?"),
])
def test_a_permissive_spf_policy_protects_nothing(record, expected):
    """+all authorises the whole internet; ?all asserts nothing about anyone."""
    policy = spf_policy(record)
    assert policy.found, "the record does exist — that was never the question"
    assert policy.all_qualifier == expected
    assert policy.protective is False
    assert policy.problems
    assert record in policy.problems[0]


def test_spf_with_no_all_mechanism_is_neutral_by_default():
    """RFC 7208: a record with no 'all' leaves unlisted senders neutral."""
    policy = spf_policy("v=spf1 include:provider.example")
    assert policy.found
    assert policy.all_qualifier is None
    assert policy.protective is False


def test_spf_redirect_delegates_rather_than_omits():
    """redirect= hands the decision to another domain, which is legitimate."""
    policy = spf_policy("v=spf1 redirect=_spf.provider.example")
    assert policy.redirect == "_spf.provider.example"
    assert policy.protective is True
    assert policy.problems == []


def test_two_spf_records_are_a_permanent_error():
    """RFC 7208 s4.5: receivers stop rather than pick a winner."""
    policy = spf_policy(["v=spf1 -all", "v=spf1 +all"])
    assert policy.record_count == 2
    assert policy.protective is False
    assert "permanent error" in policy.problems[0]


def test_spf_lookup_budget_is_counted_and_its_limits_are_stated():
    within = spf_policy("v=spf1 include:a.example include:b.example mx -all")
    assert within.lookup_count == 3
    assert within.protective
    note = dns_checks.spf_lookup_note(within)
    # The count is of what is written in the record. REACH does not expand
    # include: chains and the note has to say so rather than imply a full check.
    assert "does not expand include:" in note

    over = spf_policy("v=spf1 " + " ".join(f"include:p{i}.example" for i in range(11)) + " -all")
    assert over.lookup_count == 11
    assert over.protective is False
    assert "permanent error" in over.problems[0]


def test_spf_ignores_the_exp_modifier_in_the_lookup_count():
    """exp= points at an explanation string, and costs no lookup budget."""
    policy = spf_policy("v=spf1 include:a.example exp=why.example -all")
    assert policy.lookup_count == 1


def dkim_key(record):
    domain, selector = "sender.example", "sel"
    dns_checks.set_resolver(dns_checks.offline_resolver({
        (f"{selector}._domainkey.{domain}", "TXT"): [record]}))
    return dns_checks.analyse_dkim(domain, selector)


def test_a_published_dkim_key_is_usable():
    key = dkim_key("v=DKIM1; k=rsa; p=MIIBIjANBgkqhkiG9w0B")
    assert key.found and key.usable
    assert key.revoked is False
    assert key.key_type == "rsa"


def test_a_revoked_dkim_key_is_not_usable():
    """RFC 6376 s3.6.1: an empty p= means revoked. The record still exists,
    which is exactly why a presence check reads it as healthy."""
    key = dkim_key("v=DKIM1; k=rsa; p=")
    assert key.found, "the record is published — it just carries no key"
    assert key.revoked is True
    assert key.usable is False
    assert "revoked" in key.problems[0]


def test_dkim_key_type_is_read_from_the_record():
    assert dkim_key("v=DKIM1; k=ed25519; p=abc").key_type == "ed25519"
