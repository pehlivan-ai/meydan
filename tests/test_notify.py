"""Notifications to the team's devices: a new person is noticed where they wrote, the
notification goes with that transaction, the pusher sends it, gone devices are forgotten,
failures wait and give up, and devices subscribe over HTTP for whom a service acts."""

import json
from datetime import timedelta

import pytest
from conftest import T0, FakeChannel, msg, receive
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from meydan import notify
from meydan.auth import AuthConfig, issue_service_key
from meydan.auth.web import Auth
from meydan.notify.web import routes as notify_routes
from meydan.runtime import Runtime, RuntimeConfig

ANNA = "+15550004001"  # fake numbers: this is a public repository
BEN = "+15550004002"
SUBJECT = "mailto:ops@example.com"
DEVICE = {"p256dh": "B" + "x" * 86, "auth": "a" * 22}


def device(n: int) -> str:
    return f"https://push.example.com/send/{n}"


async def nobody(turn):
    return []


def make(db, on_receive=()):
    engine, t = db
    cfg = RuntimeConfig(debounce_s=0, max_wait_s=0, presence="instant", retry_wait_s=0)
    return Runtime(engine, t, FakeChannel(), nobody, cfg, on_receive=on_receive)


def deliveries(rt):
    d = rt.t.notify_deliveries
    with rt.engine.connect() as c:
        return c.execute(select(d.c.status, d.c.attempts).order_by(d.c.status)).all()


class Push:
    """A fake push service: answers in turn from `answers`, records what it was given."""

    def __init__(self, *answers: notify.Outcome):
        self.answers, self.got = list(answers), []

    def __call__(self, info, data):
        self.got.append((info["endpoint"], json.loads(data)))
        return self.answers.pop(0) if self.answers else notify.Outcome(201)


async def test_routing_says_when_we_met_someone(db):
    met = []
    rt = make(db, on_receive=[lambda conn, m, routed: met.append((m.text, routed.new_actor))])
    receive(rt, [msg("a1", "hello", T0, sender=ANNA)])
    receive(rt, [msg("a2", "me again", T0 + timedelta(seconds=5), sender=ANNA)])
    receive(rt, [msg("a2", "me again", T0 + timedelta(seconds=5), sender=ANNA)])  # duplicate
    assert met == [("hello", True), ("me again", False)]


async def test_a_new_person_rings_every_subscribed_device_once(db):
    def new_person(conn, m, routed):
        if routed.new_actor:
            notify.enqueue(conn, rt.t, kind="new_person", title=f"{m.sender_name} wrote",
                           body=m.text or "", url=f"/inbox/{routed.conversation_id}",
                           tag=str(routed.conversation_id))  # fmt: skip

    rt = make(db, on_receive=[new_person])
    with rt.engine.begin() as c:
        notify.subscribe(c, rt.t, "crm:user/1", endpoint=device(1), **DEVICE)
        notify.subscribe(c, rt.t, "crm:user/2", endpoint=device(2), **DEVICE)
    receive(rt, [msg("a1", "Hello, about ballet", T0, sender=ANNA)])
    receive(rt, [msg("a2", "second message", T0 + timedelta(seconds=9), sender=ANNA)])

    push = Push()
    pusher = notify.Pusher(rt.engine, rt.t, keys=notify.Keys.generate(), subject=SUBJECT, send=push)
    assert pusher.drain() == 2 and pusher.drain() == 0
    assert sorted(e for e, _ in push.got) == [device(1), device(2)]
    n = push.got[0][1]
    assert n["web_push"] == 8030
    assert n["notification"]["body"] == "Hello, about ballet"
    assert n["notification"]["navigate"].startswith("/inbox/") and n["notification"]["renotify"]
    assert deliveries(rt) == [("sent", 1), ("sent", 1)]


async def test_a_gone_device_is_forgotten_and_a_failing_one_waits_then_gives_up(db):
    rt = make(db)
    with rt.engine.begin() as c:
        notify.subscribe(c, rt.t, "u1", endpoint=device(1), **DEVICE)
        notify.subscribe(c, rt.t, "u2", endpoint=device(2), **DEVICE)
        assert notify.enqueue(c, rt.t, kind="k", title="t", body="b") == 2
    gone, busy = notify.Outcome(410, "gone"), notify.Outcome(503, "busy")
    push = Push(gone, busy)
    pusher = notify.Pusher(rt.engine, rt.t, keys=notify.Keys.generate(), subject=SUBJECT, send=push)
    assert pusher.drain() == 2
    with rt.engine.connect() as c:
        assert notify.subscribers(c, rt.t) == ["u2" if push.got[0][0] == device(1) else "u1"]
    assert deliveries(rt) == [("pending", 1)]  # waits: not due yet
    assert pusher.drain() == 0

    d = rt.t.notify_deliveries
    for _ in range(notify.ATTEMPTS):
        with rt.engine.begin() as c:
            c.execute(update(d).values(next_at=func.now()))
        push.answers.append(busy)
        pusher.drain()
    assert deliveries(rt) == [("failed", notify.ATTEMPTS)]


async def test_without_keys_nothing_is_sent_and_nothing_is_lost(db):
    rt = make(db)
    with rt.engine.begin() as c:
        notify.subscribe(c, rt.t, "u1", endpoint=device(1), **DEVICE)
        notify.enqueue(c, rt.t, kind="k", title="t", body="b")
    assert notify.Pusher(rt.engine, rt.t, keys=None, subject=SUBJECT).drain() == 0
    assert deliveries(rt) == [("pending", 0)]


async def test_one_endpoint_is_one_device_and_only_its_owner_ends_it(db):
    rt = make(db)
    with rt.engine.begin() as c:
        notify.subscribe(c, rt.t, "u1", endpoint=device(1), **DEVICE)
        notify.subscribe(c, rt.t, "u2", endpoint=device(1), **DEVICE)  # someone else signed in
        assert notify.subscribers(c, rt.t) == ["u2"]
        assert not notify.unsubscribe(c, rt.t, "u1", device(1))
        assert notify.unsubscribe(c, rt.t, "u2", device(1))


async def test_the_payload_is_encrypted_for_a_real_browser_key():
    """What a real device's keys need (RFC 8291): pywebpush encrypts our payload for them."""
    pytest.importorskip("pywebpush")
    from pywebpush import WebPusher

    browser = notify.Keys.generate()  # stands in for the device's own P-256 key
    sub = {
        "endpoint": device(9),
        "keys": {"p256dh": browser.public, "auth": "c2VjcmV0c2VjcmV0MTIzNA"},
    }
    data = json.dumps(notify.payload("Anna wrote", "x" * 1000, url="/inbox/1", tag="1"))
    body = WebPusher(sub).encode(data.encode(), content_encoding="aes128gcm")["body"]
    assert 0 < len(body) < 4096 and data.encode() not in body


async def test_http_subscribe_for_whom_a_service_acts(db):
    rt = make(db)
    keys = notify.Keys.generate()
    auth = Auth(rt.engine, rt.t, AuthConfig(audience="ops"), secure_cookies=False)
    app = FastAPI()
    app.include_router(notify_routes(auth, rt.t, rt.engine, keys=keys), prefix="/notify")
    client = TestClient(app)
    with rt.engine.begin() as c:
        _, key = issue_service_key(c, rt.t, "dash", {"ops", "notify:subscribe"}, audience="ops")
        _, other = issue_service_key(c, rt.t, "other", {"ops"}, audience="ops")
    as_ada = {"Authorization": f"Bearer {key}", "X-On-Behalf-Of": "crm:user/7"}

    assert client.get("/notify/key", headers=as_ada).json() == {"public_key": keys.public}
    assert (
        client.get("/notify/key", headers={"Authorization": f"Bearer {other}"}).status_code == 403
    )
    sub = {"endpoint": device(1), "keys": DEVICE}
    assert client.post("/notify/subscriptions", json=sub, headers=as_ada).status_code == 204
    bad = {"endpoint": "http://push.example.com/x", "keys": DEVICE}  # not https
    assert client.post("/notify/subscriptions", json=bad, headers=as_ada).status_code == 422
    with rt.engine.connect() as c:
        assert notify.subscribers(c, rt.t) == ["crm:user/7"]
    gone = client.request("DELETE", "/notify/subscriptions", json={"endpoint": device(1)},
                          headers=as_ada)  # fmt: skip
    assert gone.status_code == 204
    with rt.engine.connect() as c:
        assert notify.subscribers(c, rt.t) == []


NEW_PERSON = notify.Kind(
    name="new_person",
    label="Someone new",
    when="Someone writes for the first time.",
    example=notify.Example(
        title="Anna (example)", body="Hi, a question about classes", url="/inbox"
    ),
)


async def test_http_kinds_test_sends_the_example_only_to_my_devices_and_others_send_declared_kinds(
    db,
):
    rt = make(db)
    auth = Auth(rt.engine, rt.t, AuthConfig(audience="ops"), secure_cookies=False)
    app = FastAPI()
    app.include_router(
        notify_routes(auth, rt.t, rt.engine, keys=notify.Keys.generate(), kinds=[NEW_PERSON]),
        prefix="/notify",
    )
    client = TestClient(app)
    with rt.engine.begin() as c:
        _, key = issue_service_key(c, rt.t, "dash", {"ops", "notify:subscribe"}, audience="ops")
        _, other_system = issue_service_key(c, rt.t, "books", {"notify:send"}, audience="ops")
        notify.subscribe(c, rt.t, "crm:user/7", endpoint=device(1), **DEVICE)
        notify.subscribe(c, rt.t, "crm:user/8", endpoint=device(2), **DEVICE)
    as_ada = {"Authorization": f"Bearer {key}", "X-On-Behalf-Of": "crm:user/7"}
    books = {"Authorization": f"Bearer {other_system}"}

    assert client.get("/notify/kinds", headers=as_ada).json() == [
        {"name": "new_person", "label": "Someone new", "when": "Someone writes for the first time."}
    ]
    assert client.post("/notify/test", json={"kind": "nope"}, headers=as_ada).status_code == 404
    assert client.post("/notify/test", json={"kind": "new_person"}, headers=as_ada).json() == {
        "devices": 1
    }
    push = Push()
    notify.Pusher(rt.engine, rt.t, keys=notify.Keys.generate(), subject=SUBJECT, send=push).drain()
    [(endpoint, sent)] = push.got
    assert endpoint == device(1)  # only Ada's own device
    assert sent["notification"]["title"] == "Anna (example)"

    assert (
        client.post("/notify/test", json={"kind": "new_person"}, headers=books).status_code == 403
    )
    paid = {"kind": "payment", "title": "Paid", "body": "x"}
    assert client.post("/notify/notifications", json=paid, headers=books).status_code == 422
    away = {"kind": "new_person", "title": "x", "body": "y", "url": "https://elsewhere.example"}
    assert client.post("/notify/notifications", json=away, headers=books).status_code == 422
    ok = {"kind": "new_person", "title": "Ben", "body": "hello", "url": "/inbox/1"}
    assert client.post("/notify/notifications", json=ok, headers=books).json() == {"devices": 2}
    assert client.post("/notify/notifications", json=ok, headers=as_ada).status_code == 403
