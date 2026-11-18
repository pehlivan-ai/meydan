"""The operator: read marks per reader, taking a conversation over (the agent stays quiet),
writing as yourself, and its HTTP surface."""

import uuid
from datetime import timedelta

import pytest
from conftest import T0, FakeChannel, msg, receive, runtime
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from meydan import operator
from meydan.auth import AuthConfig, issue_service_key
from meydan.auth.web import Auth
from meydan.channels.base import Outbound
from meydan.operator.web import routes as operator_routes

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
        assert operator.unread(c, rt.t, "ada", [conv]) == {conv: 1}
        operator.mark_read(c, rt.t, conv, "ada")
        assert operator.unread(c, rt.t, "ada", [conv]) == {}
        assert operator.unread(c, rt.t, "bora", [conv]) == {
            conv: 1
        }  # someone else's mark is theirs
    receive(rt, [msg("a2", "still there?", T0, sender=ANNA)])
    with rt.engine.connect() as c:
        assert operator.unread(c, rt.t, "ada", [conv]) == {conv: 1}


async def test_a_taken_over_conversation_waits_and_the_agent_answers_after_handback(world):
    rt, _, conv = world
    with rt.engine.begin() as c:
        operator.takeover(c, rt.t, conv, "ada")
    receive(rt, [msg("a2", "anyone?", T0, sender=ANNA)])
    await rt.work_once()
    assert [a for a, _ in outs(rt, conv)] == ["agent"]  # nothing while it is taken over
    i = rt.t.inbox
    with rt.engine.connect() as c:
        waiting = c.scalar(select(i.c.processed_at).where(i.c.message_id == "a2"))
    assert waiting is None  # it waits, it is not dropped
    assert rt.health().ok  # waiting on purpose is not stuck work
    with rt.engine.begin() as c:
        assert operator.handback(c, rt.t, conv)
    await rt.work_once()
    assert outs(rt, conv)[-1] == ("agent", "re: anyone?")  # answered once handed back


async def test_a_takeover_while_the_agent_thinks_stops_its_reply(db):
    """Karşıt tur H1: the check before the turn is not enough; the reply's own commit checks."""
    holder = {}

    async def slow(turn):
        with holder["rt"].engine.begin() as c:
            operator.takeover(c, holder["rt"].t, turn.conversation_id, "ada")  # meanwhile
        return [Outbound.text_message("too late")]

    rt = runtime(db, slow, FakeChannel())
    holder["rt"] = rt
    receive(rt, [msg("b1", "hi", T0, sender=ANNA)])
    await rt.work_once()
    o = rt.t.outbox
    with rt.engine.connect() as c:
        assert c.scalar(select(func.count()).select_from(o)) == 0  # never queued


async def test_a_takeover_cancels_the_agents_unsent_replies(world):
    rt, _, conv = world
    o = rt.t.outbox
    with rt.engine.begin() as c:
        c.execute(update(o).where(o.c.conversation_id == conv).values(status="pending"))
        operator.takeover(c, rt.t, conv, "ada")
        assert c.scalar(select(o.c.status).where(o.c.conversation_id == conv)) == "cancelled"


async def test_someone_elses_takeover_is_not_overwritten_unless_asked(world):
    rt, _, conv = world
    with rt.engine.begin() as c:
        operator.takeover(c, rt.t, conv, "ada")
        with pytest.raises(operator.AlreadyTakenError):
            operator.takeover(c, rt.t, conv, "bora")
        assert operator.takeover(c, rt.t, conv, "bora", replace=True).taken_by == "bora"


async def test_writing_never_shortens_a_takeover_and_what_staff_answered_stays_answered(world):
    rt, _, conv = world
    receive(rt, [msg("a2", "a question", T0, sender=ANNA)])
    with rt.engine.begin() as c:
        operator.takeover(c, rt.t, conv, "ada")  # until handed back
        ada = operator.staff_actor(c, rt.t, "crm:user/7", "Ada")
        operator.send(c, rt, conv, "Answered by Ada.", ada, taken_by="ada")
        assert operator.takeover_of(c, rt.t, conv).until is None  # not cut to 30 minutes
        operator.handback(c, rt.t, conv)
    await rt.work_once()
    assert [a for a, _ in outs(rt, conv)] == ["agent", "staff"]  # the agent doesn't answer again


async def test_a_hold_with_an_end_ends(world):
    rt, _, conv = world
    with rt.engine.begin() as c:
        operator.takeover(c, rt.t, conv, "ada", timedelta(minutes=5))
        assert operator.is_taken_over(c, rt.t, conv)
        h = rt.t.operator_holds
        c.execute(update(h).values(until=h.c.at - timedelta(seconds=1)))  # five minutes later
        assert not operator.is_taken_over(c, rt.t, conv)


async def test_staff_write_as_themselves_and_take_it_over(world):
    rt, channel, conv = world
    with rt.engine.begin() as c:
        ada = operator.staff_actor(c, rt.t, "crm:user/7", "Ada")
        assert operator.staff_actor(c, rt.t, "crm:user/7") == ada  # the same person each time
        operator.send(c, rt, conv, "This is Ada from the team.", ada)
        assert operator.is_taken_over(c, rt.t, conv)  # writing takes it over
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
        ada = operator.staff_actor(c, rt.t, "crm:user/7", "Ada")
        with pytest.raises(operator.WindowClosedError):
            operator.send(c, rt, conv, "late", ada)
        with pytest.raises(operator.NotFoundError):
            operator.send(c, rt, uuid.uuid4(), "nobody", ada)


async def test_http_operator_with_a_service_key(world):
    rt, _, conv = world
    auth = Auth(rt.engine, rt.t, AuthConfig(audience="ops"), secure_cookies=False)
    app = FastAPI()
    labels = lambda ids: {a: {"role": "parent"} for a in ids}  # noqa: E731 - the app's words
    app.include_router(operator_routes(auth, rt, labels=labels), prefix="/operator")
    client = TestClient(app)
    with rt.engine.begin() as c:
        _, reader = issue_service_key(c, rt.t, "dash", {"ops", "conversations:read:all"},
                                      audience="ops")  # fmt: skip
        _, writer = issue_service_key(c, rt.t, "dash-w", {"ops", "conversations:read:all",
                                      "operator:write"}, audience="ops")  # fmt: skip
    as_ada = lambda key: {"Authorization": f"Bearer {key}", "X-On-Behalf-Of": "crm:user/7"}  # noqa: E731

    [row] = client.get("/operator/conversations", headers=as_ada(reader)).json()
    assert (row["unread"], row["taken_over"], row["labels"]) == (1, False, {"role": "parent"})
    assert (
        client.post(f"/operator/conversations/{conv}/read", headers=as_ada(reader)).status_code
        == 204
    )
    assert client.get("/operator/conversations", headers=as_ada(reader)).json()[0]["unread"] == 0

    msg_ = {"text": "Hello from the team", "name": "Ada"}
    path = f"/operator/conversations/{conv}/messages"
    assert (
        client.post(path, json=msg_, headers=as_ada(reader)).status_code == 403
    )  # no operator:write
    sent = client.post(path, json=msg_, headers=as_ada(writer))
    assert sent.status_code == 200 and sent.json()["taken_until"] is not None
    row = client.get("/operator/conversations", headers=as_ada(writer)).json()[0]
    assert row["taken_over"] and row["taken_by"] == "crm:user/7"
    assert (
        client.post(f"/operator/conversations/{conv}/handback", headers=as_ada(writer)).status_code
        == 204
    )
    assert not client.get("/operator/conversations", headers=as_ada(writer)).json()[0]["taken_over"]


async def test_http_one_conversation_summary_conflict_and_audit(world):
    rt, _, conv = world
    auth = Auth(rt.engine, rt.t, AuthConfig(audience="ops"), secure_cookies=False)
    app = FastAPI()
    app.include_router(operator_routes(auth, rt), prefix="/operator")
    client = TestClient(app)
    scopes = {"ops", "conversations:read:all", "operator:write"}
    with rt.engine.begin() as c:
        _, key = issue_service_key(c, rt.t, "dash", scopes, audience="ops")
    as_ = lambda who: {"Authorization": f"Bearer {key}", "X-On-Behalf-Of": who}  # noqa: E731

    one = client.get(f"/operator/conversations/{conv}", headers=as_("crm:user/7"))
    assert one.status_code == 200 and one.json()["id"] == str(conv)
    missing = uuid.uuid4()
    assert (
        client.get(f"/operator/conversations/{missing}", headers=as_("crm:user/7")).status_code
        == 404
    )
    assert (
        client.post(
            f"/operator/conversations/{missing}/read", headers=as_("crm:user/7")
        ).status_code
        == 404
    )

    take = f"/operator/conversations/{conv}/takeover"
    assert client.post(take, json={}, headers=as_("crm:user/7")).status_code == 200
    assert (
        client.post(take, json={}, headers=as_("crm:user/8")).status_code == 409
    )  # someone else has it
    assert client.post(take, json={"replace": True}, headers=as_("crm:user/8")).status_code == 200
    assert client.get("/operator/summary", headers=as_("crm:user/8")).json() == {"taken_over": 1}

    a = rt.t.audit
    with rt.engine.connect() as c:
        acts = c.execute(select(a.c.action, a.c.on_behalf_of).where(a.c.subject_id == conv)
                         .order_by(a.c.at)).all()  # fmt: skip
    assert [tuple(r) for r in acts if r.action.startswith("operator:")] == [
        ("operator:takeover", "crm:user/7"), ("operator:takeover", "crm:user/8")
    ]  # fmt: skip
