"""Bridges: an outside system's events, signed by Standard Webhooks, stored once, handled by the
app's handler in a worker (retry, dead letter), and expectations completed by such an event.
Real Postgres (see conftest). Fake names and ids only: this is a public repository."""

import base64
import json
import time
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from meydan import bridge
from meydan.bridge import Bridge, PermanentError, RejectedError, headers, sign, verify
from meydan.bridge.web import routes as bridge_routes
from meydan.runtime.expect import NotCompletedError, cancel, complete, expect

SECRET = bridge.new_secret()
OTHER = bridge.new_secret()


def body(kind="payment.succeeded", **data) -> bytes:
    return json.dumps({"type": kind, "timestamp": "2026-11-25T12:00:00Z", "data": data}).encode()


def make(db, handlers=None, secrets=None, **kw) -> Bridge:
    engine, t = db
    secrets = {"acme": SECRET} if secrets is None else secrets
    return Bridge(engine, t, secrets=secrets, handlers=handlers or {}, **kw)


def events(db):
    engine, t = db
    with engine.connect() as c:
        return c.execute(select(t.bridge_events).order_by(t.bridge_events.c.id)).all()


# --- signatures (no database) ---


def test_the_spec_reference_signature():
    # The example in Standard Webhooks' reference libraries, checked against the official one.
    got = sign("whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw", "msg_p5jXN8AQM9LWM0D4loKWxJek",
               1614265330, b'{"test": 2432232314}')  # fmt: skip
    assert got == "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def test_verify_accepts_its_own_signature_and_any_of_several_secrets():
    b = body(ref="r1")
    h = headers(SECRET, b, event_id="evt_1")
    assert verify([SECRET], h, b)[0] == "evt_1"
    assert verify([OTHER, SECRET], h, b)[0] == "evt_1"  # while a secret is rotated
    # several signatures in one header (the sender rotating), and versions we don't know
    h2 = dict(
        h,
        **{
            "webhook-signature": "v2a,abc "
            + sign(OTHER, "evt_1", int(h["webhook-timestamp"]), b)
            + " "
            + h["webhook-signature"]
        },
    )
    assert verify([SECRET], h2, b)[0] == "evt_1"


@pytest.mark.parametrize(
    "change, status",
    [
        (lambda h, b: (h, b + b" "), 401),  # the body changed after signing
        (lambda h, b: ({**h, "webhook-id": "evt_2"}, b), 401),  # another id
        (lambda h, b: ({k: v for k, v in h.items() if k != "webhook-signature"}, b), 400),
        (lambda h, b: ({**h, "webhook-timestamp": "soon"}, b), 400),
        (lambda h, b: ({**h, "webhook-signature": "v2a,xyz"}, b), 401),  # no v1 at all
    ],
)
def test_verify_refuses(change, status):
    b = body(ref="r1")
    h, b2 = change(headers(SECRET, b, event_id="evt_1"), b)
    with pytest.raises(RejectedError) as e:
        verify([SECRET], h, b2)
    assert e.value.status == status


def test_old_and_future_timestamps_are_refused():
    b = body()
    for skew in (-301, 301):
        h = headers(SECRET, b, at=int(time.time()) + skew)
        with pytest.raises(RejectedError) as e:
            verify([SECRET], h, b)
        assert e.value.status == 401
    assert verify([SECRET], headers(SECRET, b, at=int(time.time()) - 299), b)


def test_secrets_must_be_24_to_64_bytes():
    import base64

    for n in (16, 65):
        with pytest.raises(ValueError):
            bridge._key("whsec_" + base64.b64encode(b"x" * n).decode())
    assert len(bridge._key(bridge.new_secret())) == 32


# --- receiving ---


def test_an_event_is_stored_once(db):
    engine, _ = db
    br = make(db, {("acme", "payment.succeeded"): lambda c, e: None})
    b = body(ref="r1")
    h = headers(SECRET, b, event_id="evt_1")
    with engine.begin() as c:
        first = br.receive(c, "acme", h, b)
    with engine.begin() as c:
        again = br.receive(c, "acme", h, b)  # the sender retried
    assert not first.duplicate and again.duplicate
    rows = events(db)
    assert len(rows) == 1 and rows[0].status == "pending" and rows[0].type == "payment.succeeded"
    assert rows[0].payload["data"] == {"ref": "r1"}


def test_the_same_event_id_from_two_connections_is_two_events(db):
    engine, _ = db
    br = make(db, secrets={"acme": SECRET, "zeta": OTHER})
    b = body()
    with engine.begin() as c:
        br.receive(c, "acme", headers(SECRET, b, event_id="evt_1"), b)
        br.receive(c, "zeta", headers(OTHER, b, event_id="evt_1"), b)
    assert len(events(db)) == 2


def test_a_connection_speaks_only_with_its_own_secret(db):
    engine, _ = db
    br = make(db, secrets={"acme": SECRET, "zeta": OTHER})
    b = body()
    with engine.begin() as c, pytest.raises(RejectedError) as e:
        br.receive(c, "acme", headers(OTHER, b), b)
    assert e.value.status == 401
    with engine.begin() as c, pytest.raises(RejectedError) as e:
        br.receive(c, "nobody", headers(SECRET, b), b)
    assert e.value.status == 404
    assert events(db) == []


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b'{"data": {}}', b'{"type": ""}'])
def test_a_body_without_a_type_is_refused(db, raw):
    engine, _ = db
    with engine.begin() as c, pytest.raises(RejectedError) as e:
        make(db).receive(c, "acme", headers(SECRET, raw), raw)
    assert e.value.status == 400


def test_an_event_nobody_handles_is_kept_as_ignored(db):
    engine, _ = db
    br = make(db, {})
    b = body("order.shipped")
    with engine.begin() as c:
        br.receive(c, "acme", headers(SECRET, b), b)
    assert [r.status for r in events(db)] == ["ignored"]
    assert br.drain() == 0


# --- handling ---


def test_the_handler_runs_once_and_sees_the_data(db):
    seen = []
    br = make(db, {("acme", "payment.succeeded"): lambda c, e: seen.append((e.type, e.data))})
    engine, _ = db
    b = body(ref="r1", amount=1500)
    with engine.begin() as c:
        br.receive(c, "acme", headers(SECRET, b), b)
    assert br.drain() == 1 and br.drain() == 0
    assert seen == [("payment.succeeded", {"ref": "r1", "amount": 1500})]
    (row,) = events(db)
    assert row.status == "done" and row.attempts == 1 and row.handled_at is not None


def test_a_failing_handler_writes_nothing_waits_then_gives_up(db):
    engine, t = db

    def boom(conn, event):
        conn.execute(insert(t.actors).values(kind="person"))  # must be rolled back
        raise RuntimeError("the other side said nonsense")

    br = make(db, {("acme", "payment.succeeded"): boom}, attempts=3)
    b = body()
    with engine.begin() as c:
        br.receive(c, "acme", headers(SECRET, b), b)
    for tries in (1, 2, 3):
        assert br.drain() == 1
        row = events(db)[0]
        assert row.attempts == tries and "nonsense" in row.last_error
        assert br.drain() == 0  # waiting: not due yet
        with engine.begin() as c:
            c.execute(update(t.bridge_events).values(next_at=row.received_at))
    assert events(db)[0].status == "dead"
    with engine.connect() as c:
        assert c.execute(select(t.actors)).all() == []


def test_refuse_is_dead_at_once_and_retry_puts_it_back(db):
    engine, _ = db
    calls = []

    def unknown(conn, event):
        calls.append(1)
        if len(calls) == 1:
            raise PermanentError("unknown ref")

    br = make(db, {("acme", "payment.succeeded"): unknown})
    b = body()
    with engine.begin() as c:
        br.receive(c, "acme", headers(SECRET, b), b)
    br.drain()
    row = events(db)[0]
    assert row.status == "dead" and row.attempts == 1 and row.last_error == "unknown ref"
    with engine.begin() as c:
        assert br.retry(c, row.id)
    br.drain()
    assert events(db)[0].status == "done" and len(calls) == 2


# --- HTTP ---


def test_the_http_door(db):
    seen = []
    br = make(db, {("acme", "payment.succeeded"): lambda c, e: seen.append(e.event_id)})
    app = FastAPI()
    app.include_router(bridge_routes(br, max_body=1024), prefix="/connections")
    client = TestClient(app)
    b = body(ref="r1")
    h = headers(SECRET, b, event_id="evt_9")
    r = client.post("/connections/acme/webhooks", content=b, headers=h)
    assert r.status_code == 200 and r.json() == {"received": True, "duplicate": False}
    r = client.post("/connections/acme/webhooks", content=b, headers=h)
    assert r.status_code == 200 and r.json()["duplicate"] is True
    assert client.post("/connections/acme/webhooks", content=b + b" ", headers=h).status_code == 401
    assert client.post("/connections/nobody/webhooks", content=b, headers=h).status_code == 404
    big = body(pad="x" * 2000)
    assert client.post("/connections/acme/webhooks", content=big,
                       headers=headers(SECRET, big)).status_code == 413  # fmt: skip
    br.drain()
    assert seen == ["evt_9"]


# --- expectations completed by an event ---


def test_an_expectation_is_completed_once_by_its_token(db):
    engine, t = db
    with engine.begin() as c:
        exp, token = expect(c, t, kind="payment", channel="bridge", options=["paid"],
                            ttl=timedelta(days=2), key="reg-1")  # fmt: skip
    with engine.begin() as c:
        done = complete(c, t, token, kind="payment", answer="paid", evidence={"event": "evt_1"})
    assert done.state == "answered" and done.answer == "paid"
    assert done.data["evidence"] == {"event": "evt_1"}
    with engine.begin() as c, pytest.raises(NotCompletedError) as e:
        complete(c, t, token, kind="payment", answer="paid")
    assert e.value.reason == "answered"


@pytest.mark.parametrize(
    "setup, kind, answer, reason",
    [
        (None, "payment", "paid", "unknown"),  # a token we never gave
        ("invite", "payment", "paid", "unknown"),  # a token of another kind
        ("payment", "payment", "refunded", "option"),
        ("cancel", "payment", "paid", "cancelled"),
        ("late", "payment", "paid", "expired"),
    ],
)
def test_completing_refuses(db, setup, kind, answer, reason):
    engine, t = db
    token = "never-given"
    with engine.begin() as c:
        if setup:
            given = "invite" if setup == "invite" else "payment"
            exp, token = expect(
                c, t, kind=given, channel="bridge", options=["paid"], ttl=timedelta(days=1)
            )
            if setup == "cancel":
                cancel(c, t, exp.id)
            if setup == "late":
                c.execute(
                    update(t.expectations).values(expires_at=exp.expires_at - timedelta(days=2))
                )
    with engine.begin() as c, pytest.raises(NotCompletedError) as e:
        complete(c, t, token, kind=kind, answer=answer)
    assert e.value.reason == reason


# --- v2a: public-key signatures (Standard Webhooks' asymmetric scheme) ---

SPEC_V2A = {  # the spec's own example
    "signing_key": "whsk_4uKPjji/dhB8w6GW3YYDnIMYvXcdHrvH6lidTr4Z+EY=",
    "public_key": "whpk_uWKK13PfwqyOgrqT0OrWKcS2uVD7e89+oUKnkoPjx7w=",
    "endpoint_id": "ep_2fVZdC8Kx9TR4ZoCqHWt3NnLgRb",
    "event_id": "msg_2KWPBgLlAfxdpx2AI54pPJ85f4W",
    "at": 1674087231,
    "body": b'{"type":"contact.created","timestamp":"2022-11-03T20:26:10.344522Z",'
    b'"data":{"id":"1f81eb52-5198-4599-803e-771906343485"}}',
    "signature": "v2a,kG9XaxZjAEpKB8KeQQb5G1Xp73YH5Rf5gxoWe7cg1kYMiyspdtZXe9w+dAwKeOurAwxfqml"
    "OG/QR4m9s37DzAg==",
}


def test_the_spec_reference_v2a_signature():
    v = SPEC_V2A
    got = bridge.sign_v2a(v["signing_key"], v["endpoint_id"], v["event_id"], v["at"], v["body"])
    assert got == v["signature"]
    h = {"webhook-id": v["event_id"], "webhook-timestamp": str(v["at"]),
         "webhook-signature": v["signature"]}  # fmt: skip
    creds = bridge.Credentials(public_keys=(v["public_key"],), endpoint_id=v["endpoint_id"])
    assert verify(creds, h, v["body"], now=v["at"])[0] == v["event_id"]


def test_v2a_is_checked_against_the_endpoint_id_and_the_key():
    sk, pk = bridge.new_keypair()
    _, other = bridge.new_keypair()
    b = body()
    h = headers(None, b, signing_key=sk, endpoint_id="ep_1")
    assert verify(bridge.Credentials(public_keys=(other, pk), endpoint_id="ep_1"), h, b)
    for creds in (
        bridge.Credentials(public_keys=(pk,), endpoint_id="ep_2"),  # signed for another endpoint
        bridge.Credentials(public_keys=(other,), endpoint_id="ep_1"),  # another key
        bridge.Credentials(secrets=(SECRET,)),  # we only know a shared secret
    ):
        with pytest.raises(RejectedError):
            verify(creds, h, b)
    # both versions in one header: either credential is enough
    h2 = headers(SECRET, b, signing_key=sk, endpoint_id="ep_1")
    assert verify(bridge.Credentials(secrets=(SECRET,)), h2, b)
    assert verify(bridge.Credentials(public_keys=(pk,), endpoint_id="ep_1"), h2, b)


# --- connections in the database ---


def keyring():
    from meydan.vault import Keyring

    return Keyring({"k1": Keyring.new_key()}, current="k1")


def test_credentials_ours_and_theirs_from_the_table(db):
    from meydan.bridge import connections as cx

    engine, t = db
    kr = keyring()
    sk, pk = bridge.new_keypair()
    with engine.begin() as c:
        cx.ensure(c, t, "acme")
        ours = cx.generate_secret(c, t, kr, "acme", by="tester")
        cx.add_public_key(c, t, kr, "acme", pk, endpoint_id="ep_acme")
        cx.ensure(c, t, "zeta")
        cx.add_secret(c, t, kr, "zeta", OTHER)
    br = make(db, {("acme", "payment.succeeded"): lambda c, e: None}, secrets={}, keyring=kr)
    b = body()
    with engine.begin() as c:
        assert not br.receive(c, "acme", headers(ours, b), b).duplicate  # v1, our secret
        assert not br.receive(c, "acme", headers(None, b, signing_key=sk, endpoint_id="ep_acme"),
                              b).duplicate  # fmt: skip
        assert not br.receive(c, "zeta", headers(OTHER, b), b).duplicate  # v1, their secret
        with pytest.raises(RejectedError):
            br.receive(c, "zeta", headers(ours, b), b)  # acme's secret does not speak for zeta
    with engine.connect() as c:
        info = cx.listing(c, t, "acme")
        sealed = c.execute(select(t.connection_credentials.c.sealed)).scalars().all()
    assert {(i.kind, i.origin) for i in info} == {("whsec", "ours"), ("whpk", "theirs")}
    assert all(ours not in s and OTHER not in s for s in sealed)  # never in the clear


def test_rotation_keeps_the_old_secret_for_the_overlap_then_off_turns_it_all_off(db):
    from meydan.bridge import connections as cx

    engine, t = db
    kr = keyring()
    with engine.begin() as c:
        cx.ensure(c, t, "acme")
        old = cx.generate_secret(c, t, kr, "acme")
        new = cx.rotate(c, t, kr, "acme", overlap=timedelta(days=7))
        assert set(cx.credentials(c, t, kr, "acme").secrets) == {old, new}
        cx.rotate(c, t, kr, "acme", overlap=timedelta())  # the old ones end now
        assert len(cx.credentials(c, t, kr, "acme").secrets) == 1
        cx.set_status(c, t, "acme", on=False)
        assert cx.credentials(c, t, kr, "acme") is None
    br = make(db, secrets={}, keyring=kr)
    b = body()
    with engine.begin() as c, pytest.raises(RejectedError) as e:
        br.receive(c, "acme", headers(new, b), b)
    assert e.value.status == 404


def test_a_public_key_needs_an_endpoint_id_and_reseal_moves_to_the_new_key(db):
    from meydan.bridge import connections as cx
    from meydan.vault import Keyring

    engine, t = db
    k1 = Keyring.new_key()
    kr = Keyring({"k1": k1}, current="k1")
    _, pk = bridge.new_keypair()
    with engine.begin() as c:
        cx.ensure(c, t, "acme")
        with pytest.raises(ValueError):
            cx.add_public_key(c, t, kr, "acme", pk)
        secret = cx.generate_secret(c, t, kr, "acme")
    kr2 = Keyring({"k2": Keyring.new_key(), "k1": k1}, current="k2")
    with engine.begin() as c:
        assert cx.reseal(c, t, kr2) == 1
    only_new = Keyring({"k2": base64.b64encode(kr2.keys["k2"]).decode()}, current="k2")
    with engine.connect() as c:
        assert cx.credentials(c, t, only_new, "acme").secrets == (secret,)


def test_the_command_line_sends_like_the_other_side_would(db, monkeypatch, capsys):
    import httpx

    from meydan.bridge.__main__ import main

    seen = []
    br = make(db, {("acme", "payment.succeeded"): lambda c, e: seen.append(e.data)})
    app = FastAPI()
    app.include_router(bridge_routes(br), prefix="/connections")
    client = TestClient(app)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: client.post(
        url.removeprefix("http://test"), content=kw["content"], headers=kw["headers"]))  # fmt: skip
    monkeypatch.setenv("MEYDAN_BRIDGE_SECRET", SECRET)
    args = ["send", "http://test/connections/acme/webhooks", "--type", "payment.succeeded",
            "--data", '{"ref": "r9"}']  # fmt: skip
    assert main(args) == 0
    assert capsys.readouterr().out.startswith("200 ")
    br.drain()
    assert seen == [{"ref": "r9"}]
    assert main(["secret"]) == 0 and capsys.readouterr().out.startswith("whsec_")


# --- managing connections over HTTP (meydan.bridge.manage) ---


def test_managing_a_connection_is_what_the_app_allows(db):
    from meydan.auth import AuthConfig, issue_service_key
    from meydan.auth.web import Auth
    from meydan.bridge import connections as cx
    from meydan.bridge.manage import routes as manage_routes

    engine, t = db
    kr = keyring()
    with engine.begin() as c:
        cx.ensure(c, t, "acme")
        cx.ensure(c, t, "zeta")
        _, viewer = issue_service_key(c, t, "viewer", set(), audience="dev")
        _, admin = issue_service_key(c, t, "admin", set(), audience="dev")
        names = dict(c.execute(select(t.actors.c.name, t.actors.c.id)).all())
    # the app's rule: the viewer reads acme; the admin manages acme; nobody sees zeta
    rules = {(names["viewer"], "read"), (names["admin"], "read"), (names["admin"], "manage")}
    done = []

    def may(person, name, action):
        return name == "acme" and (person.actor_id, action) in rules

    def audit(conn, person, action, name):
        done.append((action, name))

    br = make(db, {("acme", "payment.succeeded"): lambda c, e: None}, secrets={}, keyring=kr)
    auth = Auth(engine, t, AuthConfig(audience="dev"), secure_cookies=False)
    app = FastAPI()
    app.include_router(
        manage_routes(br, auth, may=may, audit=audit, reveal=True), prefix="/connections"
    )
    client = TestClient(app)
    closed = FastAPI()  # the default: a stored secret is never shown again
    closed.include_router(manage_routes(br, auth, may=may, audit=audit), prefix="/connections")
    as_ = lambda key: {"Authorization": f"Bearer {key}"}  # noqa: E731 (a short alias)

    assert [c["name"] for c in client.get("/connections", headers=as_(viewer)).json()] == ["acme"]
    assert client.get("/connections/zeta", headers=as_(admin)).status_code == 404  # names private
    # reading is not managing
    assert client.post("/connections/acme/secrets", json={}, headers=as_(viewer)).status_code == 404
    made = client.post("/connections/acme/secrets", json={}, headers=as_(admin)).json()
    assert made["secret"].startswith("whsec_")
    view = client.get("/connections/acme", headers=as_(viewer)).json()
    assert "secret" not in str(view) and view["credentials"][0]["origin"] == "ours"
    cid = view["credentials"][0]["id"]
    shut = TestClient(closed).post(
        f"/connections/acme/credentials/{cid}/reveal", headers=as_(admin)
    )
    assert shut.status_code == 404
    assert client.post(f"/connections/acme/credentials/{cid}/reveal",
                       headers=as_(admin)).json()["secret"] == made["secret"]  # fmt: skip

    # the other side signs wrong once, then right: they see the refusal and the event
    wire = make_app_for(br)
    b = body(ref="r1")
    assert wire.post("/connections/acme/webhooks", content=b,
                     headers=headers(OTHER, b)).status_code == 401  # fmt: skip
    assert wire.post("/connections/acme/webhooks", content=b,
                     headers=headers(made["secret"], b)).status_code == 200  # fmt: skip
    refused = client.get("/connections/acme/rejections", headers=as_(viewer)).json()
    assert [(r["status"], r["reason"]) for r in refused] == [(401, "bad signature")]
    assert client.get("/connections/acme/events", headers=as_(viewer)).json()[0]["type"] == (
        "payment.succeeded"
    )
    off = client.post("/connections/acme/status", json={"on": False}, headers=as_(admin)).json()
    assert off["status"] == "off"
    assert done == [("secret:ours", "acme"), ("reveal", "acme"), ("off", "acme")]


def make_app_for(br) -> TestClient:
    app = FastAPI()
    app.include_router(bridge_routes(br), prefix="/connections")
    return TestClient(app)
