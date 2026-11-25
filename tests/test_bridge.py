"""Bridges: an outside system's events, signed by Standard Webhooks, stored once, handled by the
app's handler in a worker (retry, dead letter), and expectations completed by such an event.
Real Postgres (see conftest). Fake names and ids only: this is a public repository."""

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
    return Bridge(engine, t, secrets=secrets or {"acme": SECRET}, handlers=handlers or {}, **kw)


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
        raise RuntimeError("partner said nonsense")

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
    app.include_router(bridge_routes(br, max_body=1024), prefix="/partners")
    client = TestClient(app)
    b = body(ref="r1")
    h = headers(SECRET, b, event_id="evt_9")
    r = client.post("/partners/acme/webhooks", content=b, headers=h)
    assert r.status_code == 200 and r.json() == {"received": True, "duplicate": False}
    r = client.post("/partners/acme/webhooks", content=b, headers=h)
    assert r.status_code == 200 and r.json()["duplicate"] is True
    assert client.post("/partners/acme/webhooks", content=b + b" ", headers=h).status_code == 401
    assert client.post("/partners/nobody/webhooks", content=b, headers=h).status_code == 404
    big = body(pad="x" * 2000)
    assert client.post("/partners/acme/webhooks", content=big,
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
