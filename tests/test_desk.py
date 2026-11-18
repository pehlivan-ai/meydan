"""The desk: read marks per reader, holding a conversation (the agent stays quiet), writing as
yourself, and its HTTP surface."""

import uuid
from datetime import timedelta

import pytest
from conftest import T0, FakeChannel, msg, receive, runtime
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from meydan import desk
from meydan.auth import AuthConfig, issue_service_key
from meydan.auth.web import Auth
from meydan.channels.base import Outbound
from meydan.desk.web import routes as desk_routes

ANNA = "+15550003001"  # fake numbers: this is a public repository


async def agent(turn):
    return [Outbound.text_message(f"re: {turn.text}")]


@pytest.fixture
async def world(db):
    channel = FakeChannel()
    rt = runtime(db, agent, channel)
    receive(rt, [msg("a1", "hello", T0, sender=ANNA)])
    await rt.work_once()
    with rt.engine.connect() as c:
        conv = c.scalar(select(rt.t.inbox.c.conversation_id))
    return rt, channel, conv


def outs(rt, conv):
    o = rt.t.outbox
    with rt.engine.connect() as c:
        return c.execute(
            select(o.c.author, o.c.message["text"].astext).where(o.c.conversation_id == conv)
            .order_by(o.c.created_at, o.c.id)
        ).all()  # fmt: skip


async def test_each_reader_has_their_own_unread(world):
    rt, _, conv = world
    with rt.engine.begin() as c:
        assert desk.unread(c, rt.t, "ada", [conv]) == {conv: 1}
        desk.mark_read(c, rt.t, conv, "ada")
        assert desk.unread(c, rt.t, "ada", [conv]) == {}
        assert desk.unread(c, rt.t, "bora", [conv]) == {conv: 1}  # someone else's mark is theirs
    receive(rt, [msg("a2", "still there?", T0, sender=ANNA)])
    with rt.engine.connect() as c:
        assert desk.unread(c, rt.t, "ada", [conv]) == {conv: 1}


async def test_a_held_conversation_gets_no_answer_until_handed_back(world):
    rt, _, conv = world
    with rt.engine.begin() as c:
        desk.hold(c, rt.t, conv, "ada")
    receive(rt, [msg("a2", "anyone?", T0, sender=ANNA)])
    await rt.work_once()
    assert [a for a, _ in outs(rt, conv)] == ["agent"]  # only the first answer, none since
    i = rt.t.inbox
    with rt.engine.connect() as c:
        held = c.execute(select(i.c.processed_at, i.c.error).where(i.c.message_id == "a2")).one()
    assert held.processed_at is not None and held.error is None  # recorded, not an error
    with rt.engine.begin() as c:
        assert desk.release(c, rt.t, conv)
    receive(rt, [msg("a3", "hello again", T0, sender=ANNA)])
    await rt.work_once()
    assert outs(rt, conv)[-1] == ("agent", "re: hello again")


async def test_a_hold_with_an_end_ends(world):
    rt, _, conv = world
    with rt.engine.begin() as c:
        desk.hold(c, rt.t, conv, "ada", timedelta(minutes=5))
        assert desk.is_held(c, rt.t, conv)
        h = rt.t.desk_holds
        c.execute(update(h).values(until=h.c.at - timedelta(seconds=1)))  # five minutes later
        assert not desk.is_held(c, rt.t, conv)


async def test_staff_write_as_themselves_and_take_it_over(world):
    rt, channel, conv = world
    with rt.engine.begin() as c:
        ada = desk.staff_actor(c, rt.t, "crm:user/7", "Ada")
        assert desk.staff_actor(c, rt.t, "crm:user/7") == ada  # the same person each time
        desk.send(c, rt, conv, "This is Ada from the team.", ada)
        assert desk.is_held(c, rt.t, conv)  # writing takes it over
    await rt.send_once()
    assert outs(rt, conv)[-1] == ("staff", "This is Ada from the team.")
    o = rt.t.outbox
    with rt.engine.connect() as c:
        by = c.scalar(select(o.c.author_actor_id).where(o.c.author == "staff"))
        name = c.scalar(select(rt.t.actors.c.name).where(rt.t.actors.c.id == by))
    assert (by, name) == (ada, "Ada")


async def test_a_closed_window_refuses_free_text(world):
    rt, _, conv = world
    i = rt.t.inbox
    with rt.engine.begin() as c:
        c.execute(update(i).values(received_at=i.c.received_at - timedelta(hours=25)))
        ada = desk.staff_actor(c, rt.t, "crm:user/7", "Ada")
        with pytest.raises(desk.WindowClosedError):
            desk.send(c, rt, conv, "late", ada)
        with pytest.raises(desk.NotFoundError):
            desk.send(c, rt, uuid.uuid4(), "nobody", ada)


async def test_http_desk_with_a_service_key(world):
    rt, _, conv = world
    auth = Auth(rt.engine, rt.t, AuthConfig(audience="ops"), secure_cookies=False)
    app = FastAPI()
    labels = lambda ids: {a: {"role": "parent"} for a in ids}  # noqa: E731 - the app's words
    app.include_router(desk_routes(auth, rt, labels=labels), prefix="/desk")
    client = TestClient(app)
    with rt.engine.begin() as c:
        _, reader = issue_service_key(c, rt.t, "dash", {"ops", "conversations:read:all"},
                                      audience="ops")  # fmt: skip
        _, writer = issue_service_key(c, rt.t, "dash-w", {"ops", "conversations:read:all",
                                      "desk:write"}, audience="ops")  # fmt: skip
    as_ada = lambda key: {"Authorization": f"Bearer {key}", "X-On-Behalf-Of": "crm:user/7"}  # noqa: E731

    [row] = client.get("/desk/conversations", headers=as_ada(reader)).json()
    assert (row["unread"], row["held"], row["labels"]) == (1, False, {"role": "parent"})
    assert (
        client.post(f"/desk/conversations/{conv}/read", headers=as_ada(reader)).status_code == 204
    )
    assert client.get("/desk/conversations", headers=as_ada(reader)).json()[0]["unread"] == 0

    msg_ = {"text": "Hello from the team", "name": "Ada"}
    path = f"/desk/conversations/{conv}/messages"
    assert client.post(path, json=msg_, headers=as_ada(reader)).status_code == 403  # no desk:write
    sent = client.post(path, json=msg_, headers=as_ada(writer))
    assert sent.status_code == 200 and sent.json()["held_until"] is not None
    row = client.get("/desk/conversations", headers=as_ada(writer)).json()[0]
    assert row["held"] and row["held_by"] == "crm:user/7"
    assert (
        client.post(f"/desk/conversations/{conv}/release", headers=as_ada(writer)).status_code
        == 204
    )
    assert not client.get("/desk/conversations", headers=as_ada(writer)).json()[0]["held"]
