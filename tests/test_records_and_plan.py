from pathlib import Path

import pytest

from dnsrecon.plan import PlanError, build_plan
from dnsrecon.providers import AzureDnsProvider, ProviderError, Route53Provider
from dnsrecon.records import RecordsError, fqdn, load_desired
from tests.fakes import FakeRoute53, rrset, zone_apex

ROOT = Path(__file__).parent.parent
DESIRED = load_desired(ROOT / "dns" / "records.yaml")

HEADER = """
zone_id: Z0123456789EXAMPLE
zone_name: example.com
managed_scope: [app.example.com]
records:
"""


def write(tmp_path, body):
    p = tmp_path / "r.yaml"
    p.write_text(HEADER + body, encoding="utf-8")
    return p


# --- desired file ---------------------------------------------------------------


def test_repo_records_file_loads_and_normalises():
    assert DESIRED.zone_name == "example.com."
    rec = DESIRED.records[("app.example.com.", "A")]
    assert rec.values == ("192.0.2.10", "192.0.2.11")


def test_fqdn():
    assert fqdn("App.Example.com") == "app.example.com."
    assert fqdn("\\052.app.example.com.") == "*.app.example.com."


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("  - {name: app.example.com, type: NS, ttl: 60, values: [ns.example.com.]}", "never"),
        ("  - {name: app.example.com, type: SOA, ttl: 60, values: [x]}", "never"),
        ("  - {name: mail.example.com, type: A, ttl: 60, values: [192.0.2.1]}", "outside"),
        ("  - {name: notapp.example.com, type: A, ttl: 60, values: [192.0.2.1]}", "outside"),
        ("  - {name: app.example.com, type: A, ttl: 0, values: [192.0.2.1]}", "ttl"),
        ("  - {name: app.example.com, type: A, ttl: 60, values: []}", "non-empty"),
        ("  - {name: app.example.com, type: CNAME, ttl: 60, values: [a., b.]}", "exactly one"),
        ("  - {name: app.example.com, type: PTR, ttl: 60, values: [x]}", "unsupported"),
        ("  - {name: app.example.com, type: A, values: [192.0.2.1]}", "needs"),
        (
            "  - {name: app.example.com, type: A, ttl: 60, values: [192.0.2.1]}\n"
            "  - {name: APP.example.com., type: a, ttl: 60, values: [192.0.2.2]}",
            "twice",
        ),
    ],
)
def test_bad_records_rejected(tmp_path, body, match):
    with pytest.raises(RecordsError, match=match):
        load_desired(write(tmp_path, body))


def test_scope_outside_zone_rejected(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(HEADER.replace("[app.example.com]", "[app.example.org]"), encoding="utf-8")
    with pytest.raises(RecordsError, match="not inside zone"):
        load_desired(p)


# --- live listing -----------------------------------------------------------------


def test_list_follows_next_record_name():
    rrsets = zone_apex() + [
        rrset(f"h{i}.app.example.com.", "A", 60, f"192.0.2.{i}") for i in range(5)
    ]
    fake = FakeRoute53(rrsets, page_size=3)
    listed = Route53Provider(fake).list_records("Z0123456789EXAMPLE")
    assert len(listed) == 7
    assert [c["StartRecordName"] for c in fake.list_calls] == [
        None,
        "h1.app.example.com.",
        "h4.app.example.com.",
    ]


def test_batch_size_limit():
    with pytest.raises(ProviderError, match="split"):
        Route53Provider(FakeRoute53([])).apply("Z1", [{}] * 1001, "x")


def test_azure_is_a_stub():
    with pytest.raises(NotImplementedError):
        AzureDnsProvider().list_records("zone")


# --- plan -------------------------------------------------------------------------


def live():
    return zone_apex() + [
        rrset("app.example.com.", "A", 300, "192.0.2.11", "192.0.2.10"),  # same, other order
        rrset("www.app.example.com.", "CNAME", 60, "app.example.com."),  # ttl drift
        rrset("old.app.example.com.", "A", 300, "192.0.2.99"),  # not in Git
        rrset("mail.example.com.", "MX", 300, "10 mx.example.com."),  # out of scope
    ]


def test_plan_actions():
    plan = build_plan(DESIRED, live())
    got = [(c.action, c.record.name, c.record.type) for c in plan.changes]
    assert got == [
        ("CREATE", "api.example.com.", "TXT"),
        ("DELETE", "old.app.example.com.", "A"),
        ("UPSERT", "www.app.example.com.", "CNAME"),
    ]
    assert plan.deletions == 1


def test_plan_never_touches_ns_soa_or_out_of_scope():
    plan = build_plan(DESIRED, live())
    touched = {(c.record.name, c.record.type) for c in plan.changes}
    assert ("example.com.", "NS") not in touched
    assert ("example.com.", "SOA") not in touched
    assert ("mail.example.com.", "MX") not in touched


def test_in_sync_gives_empty_plan():
    rrsets = zone_apex() + [
        rrset("app.example.com.", "A", 300, "192.0.2.10", "192.0.2.11"),
        rrset("www.app.example.com.", "CNAME", 300, "app.example.com."),
        rrset("api.example.com.", "TXT", 3600, '"owner=platform@example.com"'),
    ]
    assert build_plan(DESIRED, rrsets).changes == []


def test_alias_record_in_scope_is_refused():
    alias = {
        "Name": "app.example.com.",
        "Type": "A",
        "AliasTarget": {
            "DNSName": "lb.example.com.",
            "HostedZoneId": "Z2",
            "EvaluateTargetHealth": False,
        },
    }
    with pytest.raises(PlanError, match="alias"):
        build_plan(DESIRED, zone_apex() + [alias])


def test_wildcard_from_route53_matches_desired(tmp_path):
    d = load_desired(
        write(tmp_path, '  - {name: "*.app.example.com", type: A, ttl: 60, values: [192.0.2.5]}')
    )
    rrsets = [rrset("\\052.app.example.com.", "A", 60, "192.0.2.5")]
    assert build_plan(d, rrsets).changes == []


def test_ns_and_soa_skipped_even_when_scope_is_the_whole_zone(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(
        HEADER.replace("[app.example.com]", "[example.com]")
        + "  - {name: example.com, type: A, ttl: 60, values: [192.0.2.1]}\n",
        encoding="utf-8",
    )
    d = load_desired(p)
    rrsets = zone_apex() + [
        rrset("example.com.", "A", 60, "192.0.2.1"),
        rrset("dev.example.com.", "NS", 300, "ns-9.awsdns-00.org."),  # delegation
    ]
    # Not in Git, but must never be planned for deletion.
    assert build_plan(d, rrsets).changes == []
