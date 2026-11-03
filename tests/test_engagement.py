"""The ordinal engagement scale: 0 none, 1 delivered, 2 seen, 3 answered (observe.engagement),
for templates and an agent's own replies alike. Fake numbers and ids only."""

import uuid
from datetime import timedelta

from conftest import T0, msg, receive, runtime
from sqlalchemy import select, update
from test_interactions import invite, press, status, who

from meydan.channels.base import Outbound
from meydan.observe.engagement import Engagement, engagement
from meydan.observe.query import interactions

H = timedelta(hours=1)


def test_the_scale_is_ordinal_and_horizon_aware():
    level = lambda **kw: engagement(sent_at=T0, **{"delivered_at": None, "seen_at": None,  # noqa: E731 (table-like test)
                                                  "answered_at": None, **kw})  # fmt: skip
    assert level() is Engagement.NONE
    assert level(delivered_at=T0 + H) is Engagement.DELIVERED
    assert level(delivered_at=T0, seen_at=T0 + H) is Engagement.SEEN
    # An answer is the top level even without a read receipt (receipts can be off).
    assert level(delivered_at=T0, answered_at=T0 + H) is Engagement.ANSWERED
    # Within a horizon: what came later doesn't count yet.
    assert engagement(sent_at=T0, delivered_at=T0, seen_at=T0 + 3 * H, answered_at=None,
                      within=2 * H) is Engagement.DELIVERED  # fmt: skip
    assert engagement(sent_at=None, delivered_at=None, seen_at=None, answered_at=None) is None
    assert Engagement.ANSWERED > Engagement.SEEN > Engagement.DELIVERED > Engagement.NONE


async def reply(rt, conversation_id, text, turn):
    with rt.engine.begin() as c:
        rt.enqueue(
            c, channel="whatsapp", conversation_id=conversation_id, from_number="222",
            to="+905550000001", messages=[Outbound.text_message(text)], turn_id=turn,
        )  # fmt: skip
    await rt.send_once()


def place(rt, text, sent_at):
    """Put a sent message at a known time on the database clock."""
    o = rt.t.outbox
    with rt.engine.begin() as c:
        c.execute(update(o).where(o.c.message["text"].astext == text).values(sent_at=sent_at))


async def test_an_agent_reply_is_answered_when_they_write_before_our_next_message(db):
    rt = runtime(db, lambda turn: [])
    receive(rt, [msg("w1", "selam", T0)])
    with rt.engine.connect() as c:
        [conv] = c.scalars(select(rt.t.conversations.c.id)).all()
    first, second = uuid.uuid4(), uuid.uuid4()
    await reply(rt, conv, "Merhaba!", first)
    await reply(rt, conv, "Turnuvalar şunlar", second)
    receive(rt, [msg("w2", "teşekkürler", T0 + H)])
    i = rt.t.inbox
    with rt.engine.connect() as c:
        wrote = c.scalar(select(i.c.received_at).where(i.c.message_id == "w2"))
    place(rt, "Merhaba!", wrote - H)  # before they wrote: answered (implicitly)
    place(rt, "Turnuvalar şunlar", wrote + H)  # after: nothing back yet
    with rt.engine.connect() as c:
        [a] = interactions(c, rt.t, who(rt, "ops"), turn_id=first)
        [b] = interactions(c, rt.t, who(rt, "ops"), turn_id=second)
        both = interactions(c, rt.t, who(rt, "ops"), conversation_id=conv)
    assert a.answer == "implicit" and a.engagement == Engagement.ANSWERED
    assert b.answer is None and b.engagement == Engagement.NONE
    assert {x.outbox_id for x in both} == {a.outbox_id, b.outbox_id}


async def test_explicit_beats_implicit_and_undeliverable_is_off_the_scale(db):
    rt = runtime(db, lambda turn: [])
    wamid = await invite(rt)
    receive(rt, statuses=[status(wamid, "delivered", T0)])
    receive(rt, [press("in1", wamid)])
    o = rt.t.outbox
    with rt.engine.begin() as c:
        [x] = interactions(c, rt.t, who(rt, "ops"), ref="invite:1")
        assert x.answer == "explicit" and x.engagement == Engagement.ANSWERED
        assert x.seen_bias == "under" and not x.undeliverable
        c.execute(update(o).values(status="dead", delivered_at=None))
        [y] = interactions(c, rt.t, who(rt, "ops"), ref="invite:1")
        assert y.undeliverable and y.engagement is None
