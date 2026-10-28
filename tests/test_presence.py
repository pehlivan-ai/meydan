"""Presence: "seen" shortly after arrival, then "typing…" at the turn, refreshed while slow."""

import asyncio
import time
from datetime import timedelta

import pytest
from conftest import T0, FakeChannel, msg, receive, runtime
from sqlalchemy import select, text, update

from meydan.channels.base import Outbound
from meydan.runtime import Presence


async def reply(turn):
    return [Outbound.text_message("tamam")]


def test_styles_and_validation():
    assert Presence.of("human") == Presence()
    assert Presence.of("quiet").read_after_s is None and not Presence.of("quiet").typing
    assert Presence.of(Presence(read_after_s=2)).read_after_s == 2
    with pytest.raises(ValueError):
        Presence.of("shy")


async def test_reader_marks_the_latest_message_once_after_the_delay(db):
    ch = FakeChannel()
    rt = runtime(db, reply, ch, debounce_s=60, max_wait_s=120, presence=Presence(read_after_s=1))
    receive(rt, [msg("w1", "selam", T0), msg("w2", "nasılsın", T0 + timedelta(seconds=1))])
    assert await rt.read_once() == 0  # not yet: just arrived
    with rt.engine.begin() as c:  # a second passes (database clock)
        c.execute(update(rt.t.inbox).values(received_at=text("now() - interval '2 s'")))
    assert await rt.read_once() == 1
    assert ch.events == [("read", "w2")]  # the latest one covers the burst
    assert await rt.read_once() == 0  # once
    with rt.engine.connect() as c:
        assert all(r.read_at for r in c.execute(select(rt.t.inbox)))


async def test_turn_shows_read_then_typing_with_the_gap(db):
    ch = FakeChannel()
    p = Presence(read_after_s=60, typing_after_read_s=0.3, typing_refresh_s=None)
    rt = runtime(db, reply, ch, presence=p)
    receive(rt, [msg("w1", "selam", T0)])
    start = time.monotonic()
    assert await rt.work_once() == 1  # turn before the reader: it marks read itself, then waits
    assert time.monotonic() - start >= 0.3
    assert ch.events == [("read", "w1"), ("typing", "w1")]
    with rt.engine.connect() as c:
        row = c.execute(select(rt.t.inbox)).one()
    assert row.read_at is not None and row.typing_at is not None and row.read_at <= row.typing_at


async def test_typing_is_refreshed_while_a_slow_turn_runs(db):
    async def slow(turn):
        await asyncio.sleep(0.35)
        return [Outbound.text_message("geldim")]

    ch = FakeChannel()
    rt = runtime(
        db, slow, ch, presence=Presence(read_after_s=0, typing_after_read_s=0, typing_refresh_s=0.1)
    )
    receive(rt, [msg("w1", "selam", T0)])
    await rt.work_once()
    typings = [e for e in ch.events if e[0] == "typing"]
    assert len(typings) >= 3  # first + refreshes; stopped once the reply was ready
    count = len(typings)
    await asyncio.sleep(0.25)
    assert len([e for e in ch.events if e[0] == "typing"]) == count


async def test_quiet_shows_nothing(db):
    ch = FakeChannel()
    rt = runtime(db, reply, ch, presence="quiet")
    receive(rt, [msg("w1", "selam", T0)])
    with rt.engine.begin() as c:
        c.execute(update(rt.t.inbox).values(received_at=text("now() - interval '1 h'")))
    assert await rt.read_once() == 0
    await rt.work_once()
    await rt.send_once()
    assert ch.events == [] and [s[2] for s in ch.sent] == ["tamam"]


async def test_answered_messages_get_no_separate_read_receipt(db):
    ch = FakeChannel()
    rt = runtime(db, reply, ch, presence=Presence(read_after_s=1, typing=False))
    receive(rt, [msg("w1", "selam", T0)])
    await rt.work_once()  # answered (typing off: nothing stamped read)
    with rt.engine.begin() as c:
        c.execute(update(rt.t.inbox).values(received_at=text("now() - interval '1 h'")))
    assert await rt.read_once() == 0 and ch.events == []


async def test_bubbles_stay_paced_after_a_slow_turn(db):
    async def slow_two(turn):
        await asyncio.sleep(0.5)  # thinking: the turn's transaction is older than its replies
        return [Outbound.text_message("a"), Outbound.text_message("x" * 40)]

    ch = FakeChannel()
    rt = runtime(db, slow_two, ch, chars_per_second=40, max_bubble_gap_s=3)
    receive(rt, [msg("w1", "selam", T0)])
    await rt.work_once()
    await rt.send_once()
    await rt.send_once()
    assert [s[2] for s in ch.sent] == ["a"]  # the second waits ~1 s from when it was queued
    await asyncio.sleep(1.1)
    await rt.send_once()
    assert len(ch.sent) == 2
