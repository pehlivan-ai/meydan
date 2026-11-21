"""What the model reads of our own messages (`recall`), and what is sent when a message cannot go
(`fallback`). The real message stays the source of truth for the people allowed to see it."""

import uuid

from conftest import T0, FakeChannel, msg, receive, runtime
from sqlalchemy import select

from meydan.auth import Principal
from meydan.channels.base import Outbound, SendError, StatusUpdate
from meydan.observe import query

ANNA = "+15550003001"  # a fake number: this is a public repository
SECRET = "Ada Kaya, Can Demir"  # stands for what only people may read (names in a timetable)


def who(actor_id, *scopes):
    return Principal(actor_id, uuid.uuid4(), "ops", frozenset(scopes))


def outbox(rt):
    with rt.engine.connect() as c:
        return c.execute(select(rt.t.outbox).order_by(rt.t.outbox.c.created_at)).mappings().all()


async def test_the_model_reads_the_recall_and_people_allowed_read_the_real_message(db):
    async def plan(turn):
        return [Outbound.text_message(f"Tomorrow: {SECRET}", recall="[timetable: 2 lessons]")]

    rt = runtime(db, plan, FakeChannel())
    receive(rt, [msg("a1", "plan?", T0, sender=ANNA)])
    await rt.work_once()
    await rt.send_once()
    [row] = outbox(rt)
    assert SECRET in row["message"]["text"]  # the source of truth keeps what was sent

    with rt.engine.connect() as c:
        lines = rt.transcript(c, row["conversation_id"])
    said = [line.text for line in lines if line.direction == "out"]
    assert said == ["[timetable: 2 lessons]"]  # what the model reads in later turns

    actor = row["recipient_identity_id"]
    with rt.engine.begin() as c:
        anna = c.scalar(select(rt.t.claims.c.actor_id).where(rt.t.claims.c.identity_id == actor))
        stranger = query.conversation(c, rt.t, who(uuid.uuid4()), row["conversation_id"])
        ops = query.conversation(c, rt.t, who(uuid.uuid4(), "conversations:read:all", "ops"),
                                 row["conversation_id"])  # fmt: skip
        reader = query.conversation(c, rt.t, who(anna), row["conversation_id"])
    assert stranger is None  # not allowed: not even that it exists
    assert all(line.text is None for line in ops.lines)  # allowed to list, not to read
    sent = [line.text for line in reader.lines if line.direction == "out"]
    assert sent == [f"Tomorrow: {SECRET}"]  # allowed to read: the real message


async def test_a_message_refused_for_good_goes_out_as_its_fallback(db):
    from meydan import media

    engine, t = db
    with engine.begin() as c:
        key = media.put(c, t, b"\x89PNG timetable", "image/png")
    picture = Outbound.image_message(
        key,
        alt="timetable",
        fallback=Outbound.text_message(f"Tomorrow: {SECRET}", recall="[timetable: 2 lessons]"),
    )

    async def plan(turn):
        return [picture]

    channel = FakeChannel(fail=[SendError("400 131053: media upload error", retryable=False)])
    rt = runtime(db, plan, channel)
    receive(rt, [msg("a1", "plan?", T0, sender=ANNA)])
    await rt.work_once()
    await rt.send_once()  # refused for good: the image dies, its fallback is queued
    await rt.send_once()
    rows = outbox(rt)
    assert [r["status"] for r in rows] == ["dead", "sent"]
    assert "fallback" not in rows[0]["message"]  # queued once, and not kept
    assert channel.sent[-1][2] == f"Tomorrow: {SECRET}"
    with rt.engine.connect() as c:
        lines = rt.transcript(c, rows[0]["conversation_id"])
    assert [line.text for line in lines if line.direction == "out"] == ["[timetable: 2 lessons]"]


async def test_a_message_reported_failed_later_goes_out_as_its_fallback(db):
    async def plan(turn):
        return [Outbound.text_message("hi", fallback=Outbound.text_message("hi again"))]

    channel = FakeChannel()
    rt = runtime(db, plan, channel)
    receive(rt, [msg("a1", "plan?", T0, sender=ANNA)])
    await rt.work_once()
    await rt.send_once()
    [row] = outbox(rt)
    assert row["status"] == "sent" and row["message"]["fallback"]  # might still fail: kept
    failed = StatusUpdate("whatsapp", row["channel_message_id"], "failed", T0, "131026")
    receive(rt, statuses=[failed])
    await rt.send_once()
    rows = outbox(rt)
    assert [r["status"] for r in rows] == ["failed", "sent"] and "fallback" not in rows[0][
        "message"
    ]
    assert channel.sent[-1][2] == "hi again"


async def test_a_delivered_message_forgets_its_fallback(db):
    async def plan(turn):
        return [Outbound.text_message("hi", fallback=Outbound.text_message(SECRET))]

    rt = runtime(db, plan, FakeChannel())
    receive(rt, [msg("a1", "plan?", T0, sender=ANNA)])
    await rt.work_once()
    await rt.send_once()
    [row] = outbox(rt)
    receive(rt, statuses=[StatusUpdate("whatsapp", row["channel_message_id"], "delivered", T0)])
    [row] = outbox(rt)
    assert row["status"] == "delivered" and "fallback" not in row["message"]
