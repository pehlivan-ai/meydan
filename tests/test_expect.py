"""Expectations: what we wait for, completed once by a matching answer (runtime.expect).
Real Postgres 18 (see test_runtime.py). Fake numbers and ids only."""

from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import T0, FakeChannel, msg, receive
from sqlalchemy import select, update
from test_interactions import who

from meydan.channels.base import Address, Outbound
from meydan.observe.query import interactions
from meydan.runtime import Runtime, RuntimeConfig
from meydan.runtime.expect import (
    Expectations,
    Handler,
    PendingError,
    Replies,
    ask,
    cancel,
    expect,
    get,
)

PHONE, OTHER = "+905550000001", "+905550000009"
ENDPOINT = Address("wa-pnid", "222")


def setup(db, **handler):
    """A runtime whose 'invite' answers are recorded in `seen`."""
    seen: dict[str, list] = {"answer": [], "close": []}

    def on_answer(conn, exp, routed):
        seen["answer"].append((exp.answer, routed.actor_id))
        return [Outbound.text_message(f"ok {exp.answer}")]

    def on_close(conn, exp):
        seen["close"].append(exp.state)

    engine, tables = db
    expectations = Expectations(
        tables,
        {"invite": Handler(on_answer, on_close, Replies(not_yours="değil", expired="doldu"))},
    )
    cfg = RuntimeConfig(debounce_s=0, max_wait_s=0, chars_per_second=1e9, presence="instant")
    rt = Runtime(engine, tables, FakeChannel(), lambda turn: [], cfg, expect=expectations)
    return rt, expectations, seen


async def asked(rt, key=None, on_pending="refuse", ttl=timedelta(days=7)):
    """Ask PHONE (accept/decline) and send it; returns the expectation and its button payloads."""
    with rt.engine.begin() as c:
        exp = ask(
            c, rt, kind="invite", channel="whatsapp", endpoint=ENDPOINT,
            address=Address("tel", PHONE), name="Ada", options=("accept", "decline"), ttl=ttl,
            message=lambda b: Outbound.template_message(
                "invite", "tr", buttons=(b["accept"], b["decline"])
            ),
            data={"role": "admin"}, key=key, on_pending=on_pending,
        )  # fmt: skip
    await rt.send_once()
    o = rt.t.outbox
    with rt.engine.connect() as c:
        row = c.execute(select(o).where(o.c.turn_id == exp.turn_id)).one()
    return exp, row


def press(rt, mid, button, wamid, sender=PHONE):
    m = replace(msg(mid, "Kabul", T0, sender=sender), kind="button", reply_to=wamid)
    m = replace(m, payload={"id": mid, "button": {"payload": button, "text": "Kabul"}})
    receive(rt, [m])
    o = rt.t.outbox
    with rt.engine.connect() as c:
        return c.scalars(select(o.c.message["text"].astext).order_by(o.c.created_at.desc())).first()


async def test_ask_sends_buttons_with_its_token_and_follows_the_message(db):
    rt, _, _ = setup(db)
    exp, row = await asked(rt)
    accept, decline = row.message["buttons"]
    assert accept.startswith("expect:") and accept.endswith(":accept")
    assert decline.split(":")[1] == accept.split(":")[1]  # one token, two options
    assert row.ref == f"expect:{exp.id}" and exp.state == "pending"
    with rt.engine.connect() as c:
        assert get(c, rt.t, exp.id).turn_id == row.turn_id
        [x] = interactions(c, rt.t, who(rt, "ops"), ref=f"expect:{exp.id}")
    assert x.template == "invite"


async def test_the_owner_answers_once(db):
    rt, _, seen = setup(db)
    exp, row = await asked(rt)
    accept, decline = row.message["buttons"]
    assert press(rt, "in1", accept, row.channel_message_id) == "ok accept"
    assert press(rt, "in2", decline, row.channel_message_id) == "This was already answered."
    assert [a for a, _ in seen["answer"]] == ["accept"]  # the app acted once
    with rt.engine.connect() as c:
        done = get(c, rt.t, exp.id)
    assert done.state == "answered" and done.answer == "accept" and done.answered_by is not None


async def test_someone_else_cannot_answer(db):
    rt, _, seen = setup(db)
    receive(rt, [msg("w0", "selam", T0, sender=PHONE)])  # PHONE is a known person
    exp, row = await asked(rt)
    assert press(rt, "in1", row.message["buttons"][0], row.channel_message_id, OTHER) == "değil"
    with rt.engine.connect() as c:
        assert get(c, rt.t, exp.id).state == "pending" and seen["answer"] == []


async def test_expired_cancelled_and_borrowed_tokens(db):
    rt, _, seen = setup(db)
    exp, row = await asked(rt)
    e = rt.t.expectations
    with rt.engine.begin() as c:
        c.execute(update(e).values(expires_at=T0))  # long past
    assert press(rt, "in1", row.message["buttons"][0], row.channel_message_id) == "doldu"
    assert seen["close"] == ["expired"]
    exp2, row2 = await asked(rt)
    with rt.engine.begin() as c:
        assert cancel(c, rt.t, exp2.id)
    assert press(rt, "in2", row2.message["buttons"][0], row2.channel_message_id) == (
        "This was cancelled."
    )
    exp3, row3 = await asked(rt)
    # The right token pressed on another message of ours: not this expectation's message.
    got = press(rt, "in3", row3.message["buttons"][0], row2.channel_message_id)
    assert got == "This is no longer valid."
    assert press(rt, "in4", "expect:nope:accept", None) == "This is no longer valid."


async def test_on_pending_refuse_resend_allow(db):
    rt, _, _ = setup(db)
    first, row = await asked(rt, key="admin:+905550000001")
    with pytest.raises(PendingError) as e:
        await asked(rt, key="admin:+905550000001")
    assert e.value.expectation_id == first.id
    again, row2 = await asked(rt, key="admin:+905550000001", on_pending="resend")
    assert again.id == first.id  # the same thing, asked again
    # The first message's buttons carry the old token: it no longer works.
    assert press(rt, "in1", row.message["buttons"][0], row.channel_message_id) == (
        "This is no longer valid."
    )
    other, _ = await asked(rt, key="admin:+905550000001", on_pending="allow")
    assert other.id != first.id


async def test_sweep_closes_overdue_and_undeliverable(db):
    rt, expectations, seen = setup(db)
    late, _ = await asked(rt)
    lost, lost_row = await asked(rt)
    e, o = rt.t.expectations, rt.t.outbox
    with rt.engine.begin() as c:
        c.execute(update(e).where(e.c.id == late.id).values(expires_at=T0))
        c.execute(update(o).where(o.c.id == lost_row.id).values(status="dead"))
    assert expectations.sweep_once(rt.engine) == 2
    assert sorted(seen["close"]) == ["expired", "undeliverable"]
    assert expectations.sweep_once(rt.engine) == 0  # closed once
    with rt.engine.connect() as c:
        assert get(c, rt.t, lost.id).state == "undeliverable"


def test_expect_without_sending(db):
    engine, t = db
    with engine.begin() as c:
        exp, token = expect(
            c, t, kind="rsvp", channel="whatsapp", options=("yes", "no"), ttl=timedelta(hours=1)
        )
        assert exp.identity_id is None and len(token) > 10
