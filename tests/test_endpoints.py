"""Our own addresses (runtime.endpoints): numbers from the channel, email domains from DNS.
Real Postgres 18 for the store (see test_runtime.py). Fake numbers, ids and domains only."""

import json
from datetime import timedelta

import httpx
import pytest
from conftest import T0

from meydan.channels.base import Address, EndpointUpdate
from meydan.channels.email import checks, dns_health
from meydan.channels.whatsapp import GraphError, WhatsAppCloud, number_events
from meydan.cli import main
from meydan.runtime import endpoints

PNID = Address("wa-pnid", "111")


def number(**kw):
    n = {
        "id": "111", "display_phone_number": "+90 555 000 00 01", "verified_name": "Example",
        "status": "CONNECTED", "quality_rating": "GREEN", "name_status": "APPROVED",
        "code_verification_status": "VERIFIED", "platform_type": "CLOUD_API",
        "messaging_limit_tier": "TIER_250", "throughput": {"level": "STANDARD"},
    }  # fmt: skip
    return n | kw


def cloud(pages, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.method == "POST":
            if request.url.path.endswith("/register") and json.loads(request.content)["pin"] == "0":
                return httpx.Response(400, json={"error": {"code": 100, "message": "bad pin"}})
            return httpx.Response(200, json={"id": "222", "success": True})
        after = request.url.params.get("after")
        return httpx.Response(200, json=pages[after or ""])

    return WhatsAppCloud("token", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_numbers_are_listed_with_their_checks_across_pages():
    pages = {
        "": {"data": [number()], "paging": {"cursors": {"after": "p2"}, "next": "https://x"}},
        "p2": {"data": [number(id="333", status="PENDING", quality_rating="UNKNOWN",
                               name_status="PENDING_REVIEW", platform_type="NOT_APPLICABLE",
                               code_verification_status="NOT_VERIFIED")]},
    }  # fmt: skip
    ok, new = await cloud(pages).list_numbers("waba")
    assert ok.address == PNID and ok.limit == "TIER_250" and ok.account == "waba"
    assert ok.details["throughput"] == "STANDARD"
    assert endpoints.worst(endpoints.checks_of(ok.details)) == "ok"
    levels = {c.name: c.level for c in endpoints.checks_of(new.details)}
    assert levels == {"status": "warn", "quality": "ok", "name": "warn",
                      "verification": "warn", "platform": "warn"}  # fmt: skip


async def test_red_or_banned_numbers_fail():
    pages = {"": {"data": [number(quality_rating="RED"), number(id="9", status="BANNED")]}}
    red, banned = await cloud(pages).list_numbers("waba")
    assert endpoints.worst(endpoints.checks_of(red.details)) == "fail"
    assert endpoints.worst(endpoints.checks_of(banned.details)) == "fail"


async def test_bringing_a_number_in_and_graph_errors():
    seen: list[httpx.Request] = []
    c = cloud({}, seen)
    assert await c.add_number("waba", "90", "5550000001", "Example") == "222"
    await c.request_code("222", "VOICE", "tr")
    await c.verify_code("222", "123456")
    await c.register("222", "123456")
    paths = [(r.method, r.url.path.split("/", 2)[2]) for r in seen]
    assert paths == [("POST", "waba/phone_numbers"), ("POST", "222/request_code"),
                     ("POST", "222/verify_code"), ("POST", "222/register")]  # fmt: skip
    assert json.loads(seen[1].content) == {"code_method": "VOICE", "language": "tr"}
    assert "token" not in str(seen[0].url)  # the token goes in a header only
    with pytest.raises(GraphError) as e:
        await c.register("222", "0")
    assert e.value.code == 100 and "token" not in str(e.value)


def test_number_webhooks_name_the_account_to_sync():
    payload = {"entry": [
        {"id": "waba", "changes": [{"field": "phone_number_quality_update", "value": {}}]},
        {"id": "other", "changes": [{"field": "messages", "value": {}}]},
    ]}  # fmt: skip
    assert number_events(payload) == {"waba"}


def test_the_latest_report_wins_and_details_merge(db):
    engine, t = db

    def report(at, **kw):
        return EndpointUpdate("whatsapp", PNID, at, account="waba", **kw)

    with engine.begin() as c:
        first = {"checks": [], "name_status": "APPROVED"}
        endpoints.record(c, t, report(T0, status="CONNECTED", quality="GREEN", details=first))
        warn = {"checks": [{"name": "quality", "level": "warn"}]}
        endpoints.record(c, t, report(T0 + timedelta(hours=1), quality="YELLOW", details=warn))
        endpoints.record(c, t, report(T0 - timedelta(hours=1), quality="RED"))  # older: ignored
        got = endpoints.get(c, t, PNID)
        assert endpoints.endpoints(c, t, "email") == []
    assert got.status == "CONNECTED" and got.quality == "YELLOW"  # kept, newer
    assert got.details["name_status"] == "APPROVED" and got.health == "warn"


async def test_sync_records_every_number(db):
    engine, t = db
    pages = {"": {"data": [number(), number(id="333")]}}
    assert await endpoints.sync(engine, t, cloud(pages), "waba") == 2
    with engine.connect() as c:
        assert [e.address.value for e in endpoints.endpoints(c, t)] == ["111", "333"]


DNS = {
    ("example.com", "MX"): ["10 mx.example.net."],
    ("example.com", "TXT"): ["v=spf1 include:_spf.example.net ~all", "site-verification=x"],
    ("sel._domainkey.example.com", "TXT"): ["v=DKIM1; k=rsa; p=MIIB"],
    ("_dmarc.example.com", "TXT"): ["v=DMARC1; p=none; rua=mailto:dmarc@example.com"],
}


def lookup(records):
    return lambda name, rdtype: records.get((name, rdtype), [])


def test_email_domain_checks():
    found = {c["name"]: c["level"] for c in checks("example.com", lookup(DNS), ["sel", "gone"])}
    assert found == {"mx": "ok", "spf": "ok", "dkim:sel": "ok", "dkim:gone": "fail",
                     "dmarc": "warn", "reports": "ok"}  # fmt: skip
    enforced = DNS | {("_dmarc.example.com", "TXT"): ["v=DMARC1; p=quarantine"]}
    u = dns_health("Hello@Example.com", selectors=["sel"], lookup=lookup(enforced))
    assert u.address == Address("mailto", "hello@example.com") and u.account == "example.com"
    assert {c.name: c.level for c in endpoints.checks_of(u.details)}["reports"] == "warn"


def test_broken_email_domains_fail():
    two_spf = {("example.com", "TXT"): ["v=spf1 -all", "v=spf1 ~all"]}
    found = {c["name"]: c["level"] for c in checks("example.com", lookup(two_spf))}
    assert found == {"mx": "fail", "spf": "fail", "dkim": "warn", "dmarc": "fail"}
    with pytest.raises(ValueError):
        dns_health("not-an-address", lookup=lookup({}))


def test_cli_needs_a_token(monkeypatch):
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="WHATSAPP_ACCESS_TOKEN"):
        main(["numbers", "list", "--account", "waba"])


async def test_read_model_needs_operations(db):
    import uuid

    from meydan.auth import Principal
    from meydan.observe import query

    engine, t = db
    await endpoints.sync(engine, t, cloud({"": {"data": [number(quality_rating="YELLOW")]}}), "w")
    ops = Principal(uuid.uuid4(), uuid.uuid4(), "ops", frozenset({"ops"}))
    nobody = Principal(uuid.uuid4(), uuid.uuid4(), "app", frozenset())
    with engine.connect() as c:
        [row] = query.endpoints(c, t, ops)
        with pytest.raises(query.NotAllowedError):
            query.endpoints(c, t, nobody)
    assert row.address == "wa-pnid:111" and row.health == "warn"
    assert [x.name for x in row.checks if x.level == "warn"] == ["quality"]
