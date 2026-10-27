"""Runtime tests against a real Postgres 18 (advisory locks, SKIP LOCKED, uuidv7); see conftest."""

import asyncio
from datetime import timedelta

from conftest import T0, FakeChannel, msg, receive, runtime
from sqlalchemy import select, text, update

from meydan.channels.base import Outbound, SendError, StatusUpdate
from meydan.runtime import Runtime, RuntimeConfig, Turn


async def echo(turn: Turn):
    return [Outbound.text_message(f"got: {turn.text}")]


async def test_duplicate_webhooks_are_stored_once(db):
    rt = runtime(db, echo)
    assert receive(rt, [msg("w1", "selam", T0)]) == 1
    assert receive(rt, [msg("w1", "selam", T0)]) == 0


async def test_burst_becomes_one_turn_in_the_senders_order(db):
    seen: list[Turn] = []

    async def handler(turn):
        seen.append(turn)
        return []

    rt = runtime(db, handler)
    # Arrive out of order; the person sent "selam" first.
    receive(rt, [msg("w2", "turnuva var mı", T0 + timedelta(seconds=2))])
    receive(rt, [msg("w1", "selam", T0)])
    assert await rt.work_once() == 1
    assert seen[0].text == "selam\nturnuva var mı"
    assert await rt.work_once() == 0  # consumed


async def test_debounce_waits_for_the_person_to_stop_typing(db):
    rt = runtime(db, echo, debounce_s=60, max_wait_s=120)
    receive(rt, [msg("w1", "selam", T0)])
    assert await rt.work_once() == 0


async def test_one_turn_at_a_time_per_conversation(db):
    running, max_running = 0, 0

    async def slow(turn):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.3)
        running -= 1
        return []

    a, b = runtime(db, slow), runtime(db, slow)  # two workers, same database
    receive(a, [msg("w1", "selam", T0)])
    await asyncio.gather(a.work_once(), b.work_once())
    assert max_running == 1


async def test_replies_go_out_in_order_from_the_number_written_to(db):
    async def three(turn):
        return [Outbound.text_message(t) for t in ("bir", "iki", "üç")]

    ch = FakeChannel()
    rt = runtime(db, three, ch)
    receive(rt, [msg("w1", "selam", T0, to="333")])
    await rt.work_once()
    for _ in range(3):
        await rt.send_once()
    assert [s[2] for s in ch.sent] == ["bir", "iki", "üç"]
    assert {s[0] for s in ch.sent} == {"333"}
    assert ch.typing_for == ["w1"]


async def test_head_of_line_blocks_until_retry_then_dead_letter(db, monkeypatch):
    monkeypatch.setattr(Runtime, "_backoff", staticmethod(lambda attempt, min_wait_s=0: 0))
    ch = FakeChannel(fail=[SendError("503", retryable=True)] * 2)
    rt = runtime(db, lambda t: _two(), ch, max_send_attempts=2, batch=1)
    receive(rt, [msg("w1", "selam", T0)])
    await rt.work_once()
    await rt.send_once()  # first bubble fails (attempt 1), second must not overtake it
    assert ch.sent == []
    await rt.send_once()  # attempt 2 fails -> dead letter
    assert ch.sent == []
    await rt.send_once()  # now the next bubble may go
    assert [s[2] for s in ch.sent] == ["second"]
    _, tables = db
    with rt.engine.connect() as c:
        statuses = [r.status for r in c.execute(select(tables.outbox).order_by(tables.outbox.c.id))]
    assert statuses == ["dead", "sent"]


async def _two():
    return [Outbound.text_message("first"), Outbound.text_message("second")]


async def test_permanent_errors_dead_letter_immediately(db):
    ch = FakeChannel(fail=[SendError("131047 outside window", retryable=False)])
    rt = runtime(db, echo, ch)
    receive(rt, [msg("w1", "selam", T0)])
    await rt.work_once()
    await rt.send_once()
    _, tables = db
    with rt.engine.connect() as c:
        row = c.execute(select(tables.outbox)).one()
    assert row.status == "dead" and row.attempts == 1 and "131047" in row.last_error


async def test_receipts_only_move_forward(db):
    ch = FakeChannel()
    rt = runtime(db, echo, ch)
    receive(rt, [msg("w1", "selam", T0)])
    await rt.work_once()
    await rt.send_once()
    receive(rt, statuses=[StatusUpdate("whatsapp", "wamid.1", "read", T0)])
    receive(rt, statuses=[StatusUpdate("whatsapp", "wamid.1", "delivered", T0)])  # late, stale
    _, tables = db
    with rt.engine.connect() as c:
        assert c.scalar(select(tables.outbox.c.status)) == "read"


async def test_poison_turn_gives_up_and_sends_failure_reply(db):
    async def broken(turn):
        raise RuntimeError("model down")

    engine, tables = db
    ch = FakeChannel()
    rt = Runtime(
        engine,
        tables,
        ch,
        broken,
        RuntimeConfig(debounce_s=0, max_wait_s=0, max_turn_attempts=2),
        on_failure=lambda turn, exc: [Outbound.text_message("Bir sorun oldu, birazdan dene.")],
    )
    receive(rt, [msg("w1", "selam", T0)])
    await rt.work_once()  # attempt 1
    await rt.work_once()  # attempt 2 -> give up
    assert await rt.work_once() == 0
    await rt.send_once()
    assert [s[2] for s in ch.sent] == ["Bir sorun oldu, birazdan dene."]
    with engine.connect() as c:
        row = c.execute(select(tables.inbox)).one()
    assert row.processed_at is not None and row.error.startswith("gave up")


async def test_bubbles_are_paced(db):
    async def two(turn):
        return [Outbound.text_message("a"), Outbound.text_message("x" * 80)]

    ch = FakeChannel()
    rt = runtime(db, two, ch, chars_per_second=40, max_bubble_gap_s=3)
    receive(rt, [msg("w1", "selam", T0)])
    await rt.work_once()
    await rt.send_once()
    await rt.send_once()
    assert [s[2] for s in ch.sent] == ["a"]  # second waits ~2 s (80 chars at 40/s)
    _, tables = db
    with rt.engine.begin() as c:
        c.execute(update(tables.outbox).values(next_attempt_at=text("now() - interval '1 s'")))
    await rt.send_once()
    assert len(ch.sent) == 2


async def test_typing_failure_does_not_break_the_turn(db):
    class BrokenTyping(FakeChannel):
        async def typing(self, from_number, message_id):
            raise ValueError("bad header")

    ch = BrokenTyping()
    rt = runtime(db, echo, ch)
    receive(rt, [msg("w1", "selam", T0)])
    assert await rt.work_once() == 1
    await rt.send_once()
    assert [s[2] for s in ch.sent] == ["got: selam"]
