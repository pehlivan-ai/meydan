"""What came of the messages we sent: refs, receipt times, answers linked by the channel's
`context.id` (see test_runtime.py for the Postgres setup). Fake numbers and ids only."""

import uuid
from dataclasses import replace
from datetime import timedelta

from conftest import T0, msg, receive, runtime
from sqlalchemy import select

from meydan.auth import Principal
from meydan.channels.base import Address, Outbound, StatusUpdate
from meydan.observe.query import interactions
from meydan.runtime import replied_to
from meydan.runtime.identity import new_actor, plan_contact


async def noop(turn):
    return []


def who(rt, *scopes):
    with rt.engine.begin() as c:
        actor = new_actor(c, rt.t, "person", "Ops")
    return Principal(actor, uuid.uuid4(), "ops", frozenset(scopes))


async def invite(rt, ref="invite:1"):
    """Queue a template with buttons to a new number and send it; returns its wamid."""
    with rt.engine.begin() as c:
        p = plan_contact(c, rt.t, channel="whatsapp", to=Address("wa-pnid", "222"),
                         address=Address("tel", "+905550000001"), name="Ada")  # fmt: skip
        rt.enqueue(
            c, channel="whatsapp", conversation_id=p.conversation_id, from_number="222",
            to="+905550000001", recipient_identity_id=p.identity_id,
            messages=[Outbound.template_message("invite", "tr", buttons=("yes", "no"), ref=ref)],
        )  # fmt: skip
    await rt.send_once()
    with rt.engine.connect() as c:
        return c.scalar(select(rt.t.outbox.c.channel_message_id).where(rt.t.outbox.c.ref == ref))


def status(mid, s, at):
    return StatusUpdate("whatsapp", mid, s, at)


def press(mid, reply_to, text="Evet", kind="button", at=T0):
    return replace(msg(mid, text, at), kind=kind, reply_to=reply_to)


async def test_receipts_keep_their_times_in_any_order(db):
    rt = runtime(db, noop)
    wamid = await invite(rt)
    read, delivered = T0 + timedelta(minutes=5), T0 + timedelta(minutes=1)
    receive(rt, statuses=[status(wamid, "read", read)])  # read arrives first
    receive(rt, statuses=[status(wamid, "delivered", delivered)])
    receive(rt, statuses=[status(wamid, "read", read + timedelta(minutes=9))])  # again, later
    with rt.engine.connect() as c:
        [row] = interactions(c, rt.t, who(rt, "ops"), ref="invite:1")
    assert row.status == "read" and row.template == "invite"
    assert row.delivered_at == read  # a read is a delivery; the later "delivered" is older news
    assert row.read_at == read  # the first read counts


async def test_answers_link_to_what_they_answer(db):
    rt = runtime(db, noop)
    wamid = await invite(rt)
    receive(rt, [press("in1", wamid, at=T0 + timedelta(minutes=2))])
    receive(rt, [press("in2", wamid, text="tam zamanında", kind="text", at=T0 + timedelta(3))])
    receive(rt, [press("in3", None, text="alakasız")])  # answers nothing of ours
    with rt.engine.connect() as c:
        [row] = interactions(c, rt.t, who(rt, "ops"), ref_prefix="invite:")
        assert [(a.kind, a.text) for a in row.answers] == [("button", "Evet"), ("text", None)]
        assert row.to is not None and row.to != "+905550000001"  # masked without the scope
        found = replied_to(c, rt.t, press("in1", wamid))
        assert found is not None and found.ref == "invite:1"
        assert replied_to(c, rt.t, press("x", "wamid.unknown")) is None
        assert replied_to(c, rt.t, press("x", None)) is None


async def test_free_text_answers_need_the_message_scope_and_are_audited(db):
    rt = runtime(db, noop)
    wamid = await invite(rt)
    receive(rt, [press("in1", wamid, text="yarın bakarım", kind="text")])
    reader = who(rt, "ops", "messages:read", "addresses:read")
    with rt.engine.begin() as c:
        [row] = interactions(c, rt.t, reader, template="invite")
        assert row.answers[0].text == "yarın bakarım" and row.to == "+905550000001"
        actions = c.scalars(select(rt.t.audit.c.action)).all()
    assert sorted(actions) == ["read:addresses", "read:messages"]
