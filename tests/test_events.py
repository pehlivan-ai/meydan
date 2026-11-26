"""meydan.events: a fact written where it happened reaches every matching subscriber, each at its
own pace; at least once, retried with backoff, dead after max_attempts, never stuck."""

import asyncio

import pytest
from sqlalchemy import func, select, text, update

from meydan import events
from meydan.events import Events, Later, PermanentError, Subscriber, emit


class Recorder:
    """A handler that records what it got and does what it's told."""

    def __init__(self, *outcomes):
        self.seen, self.outcomes = [], list(outcomes)

    async def __call__(self, event):
        self.seen.append(event)
        if self.outcomes:
            out = self.outcomes.pop(0)
            if isinstance(out, Exception):
                raise out
            return out
        return None


def deliveries(engine, t, subscriber=None):
    d = t.event_deliveries
    q = select(d).order_by(d.c.next_at)
    if subscriber:
        q = q.where(d.c.subscriber == subscriber)
    with engine.connect() as c:
        return c.execute(q).all()


def due_now(engine, t):
    """Time passes: every pending delivery is due."""
    with engine.begin() as c:
        c.execute(update(t.event_deliveries).values(next_at=text("now() - interval '1 s'")))


async def test_a_fact_reaches_every_matching_subscriber_once(db):
    engine, t = db
    a, b, other = Recorder(), Recorder(), Recorder()
    ev = Events(engine, t, [
        Subscriber("a", ("thing.done",), a),
        Subscriber("b", ("thing.done", "thing.undone"), b),
        Subscriber("other", ("else",), other),
    ])  # fmt: skip
    ev.register()
    with engine.begin() as c:
        eid = emit(c, t, "thing.done", data={"n": 1})
    assert await ev.drain_once() == 2
    assert [e.id for e in a.seen] == [eid] == [e.id for e in b.seen] and other.seen == []
    assert a.seen[0].data == {"n": 1} and a.seen[0].attempt == 1
    assert await ev.drain_once() == 0  # done
    assert {r.status for r in deliveries(engine, t)} == {"done"}


async def test_nothing_happens_if_the_transaction_rolls_back(db):
    engine, t = db
    ev = Events(engine, t, [Subscriber("a", ("thing.done",), Recorder())])
    ev.register()
    with pytest.raises(RuntimeError), engine.begin() as c:
        emit(c, t, "thing.done")
        raise RuntimeError("the change it belongs to failed")
    assert await ev.drain_once() == 0
    with engine.connect() as c:
        assert c.scalar(select(func.count()).select_from(t.events)) == 0


async def test_a_key_records_the_fact_once(db):
    engine, t = db
    a = Recorder()
    ev = Events(engine, t, [Subscriber("a", ("paid",), a)])
    ev.register()
    with engine.begin() as c:
        first = emit(c, t, "paid", key="ref-1")
        assert emit(c, t, "paid", key="ref-1") is None  # the same fact again
        assert emit(c, t, "paid", key="ref-2") != first
    assert await ev.drain_once() == 2


async def test_a_channel_subscriber_gets_its_channel_and_channel_less_facts(db):
    engine, t = db
    wa, every = Recorder(), Recorder()
    ev = Events(engine, t, [
        Subscriber("wa", ("message.ingested",), wa, channel="whatsapp"),
        Subscriber("every", ("message.ingested",), every),
    ])  # fmt: skip
    ev.register()
    with engine.begin() as c:
        emit(c, t, "message.ingested", channel="whatsapp")
        emit(c, t, "message.ingested", channel="telegram")
        emit(c, t, "message.ingested")
    await ev.drain_once()
    assert sorted(str(e.channel) for e in wa.seen) == ["None", "whatsapp"]
    assert len(every.seen) == 3


async def test_a_delay_holds_a_delivery_until_its_time(db):
    engine, t = db
    a = Recorder()
    ev = Events(engine, t, [Subscriber("a", ("x",), a, delay_s=60)])
    ev.register()
    with engine.begin() as c:
        emit(c, t, "x")
    assert await ev.drain_once() == 0
    due_now(engine, t)
    assert await ev.drain_once() == 1


async def test_later_puts_it_off_without_counting_an_attempt(db):
    engine, t = db
    a = Recorder(Later(30))
    ev = Events(engine, t, [Subscriber("a", ("x",), a)])
    ev.register()
    with engine.begin() as c:
        emit(c, t, "x")
    await ev.drain_once()
    [row] = deliveries(engine, t)
    assert (row.status, row.attempts) == ("pending", 0)
    due_now(engine, t)
    await ev.drain_once()
    assert [e.attempt for e in a.seen] == [1, 1]
    assert deliveries(engine, t)[0].status == "done"


async def test_a_failure_is_retried_with_backoff_then_dead(db):
    engine, t = db
    a = Recorder(RuntimeError("down"), RuntimeError("down"))
    ev = Events(engine, t, [Subscriber("a", ("x",), a, max_attempts=2)])
    ev.register()
    with engine.begin() as c:
        emit(c, t, "x")
    await ev.drain_once()
    [row] = deliveries(engine, t)
    assert row.status == "pending" and row.attempts == 1 and "down" in row.last_error
    with engine.connect() as c:  # backoff: not before ~17 s
        assert c.scalar(select(row.next_at - func.clock_timestamp())).total_seconds() > 10
    due_now(engine, t)
    await ev.drain_once()
    [row] = deliveries(engine, t)
    assert (row.status, row.attempts) == ("dead", 2) and row.done_at is not None
    with engine.begin() as c:  # fixed: put it back
        assert events.retry(c, t, row.id)
    await ev.drain_once()
    assert deliveries(engine, t)[0].status == "done"


async def test_a_permanent_error_is_dead_at_once_with_its_reason(db):
    engine, t = db
    ev = Events(engine, t, [Subscriber("a", ("x",), Recorder(PermanentError("no such ref")))])
    ev.register()
    with engine.begin() as c:
        emit(c, t, "x")
    await ev.drain_once()
    [row] = deliveries(engine, t)
    assert (row.status, row.last_error) == ("dead", "no such ref")


async def test_one_failing_subscriber_never_holds_up_another(db):
    engine, t = db
    good = Recorder()
    ev = Events(engine, t, [
        Subscriber("bad", ("x",), Recorder(RuntimeError("x"))),
        Subscriber("good", ("x",), good),
    ])  # fmt: skip
    ev.register()
    with engine.begin() as c:
        emit(c, t, "x")
    await ev.drain_once()
    assert len(good.seen) == 1 and deliveries(engine, t, "good")[0].status == "done"


async def test_a_claim_whose_lease_ran_out_is_taken_back_and_its_late_finish_ignored(db):
    engine, t = db

    async def hangs(event):
        await asyncio.sleep(10)

    ev = Events(engine, t, [Subscriber("a", ("x",), hangs, lease_s=0.05)])
    ev.register()
    with engine.begin() as c:
        emit(c, t, "x")
    await ev.drain_once()  # the handler hits its time limit: tried again later
    [row] = deliveries(engine, t)
    assert row.status == "pending" and "Timeout" in row.last_error
    # a process died mid-handler: its claim is still "running", the lease runs out
    due_now(engine, t)
    sub = ev.subscribers["a"]
    [claimed] = ev._claim(sub)
    with engine.begin() as c:
        c.execute(update(t.event_deliveries).values(lease_until=text("now() - interval '1 s'")))
    assert ev.rescue() == 1
    ev._finish(claimed, "done", None)  # the dead process's late word changes nothing
    assert deliveries(engine, t)[0].status == "pending"


async def test_subscriptions_are_rows_so_another_process_gets_the_fact(db):
    engine, t = db
    ops = Recorder()
    Events(engine, t, [Subscriber("ops.notify", ("x",), ops)]).register()  # the ops process
    with engine.begin() as c:  # the app process emits, knowing nothing of ops
        emit(c, t, "x")
    assert await Events(engine, t, [Subscriber("ops.notify", ("x",), ops)]).drain_once() == 1
    with engine.begin() as c:
        assert events.unsubscribe(c, t, "ops.notify")
        assert c.scalar(select(func.count()).select_from(t.event_deliveries)) == 0


async def test_finished_work_and_old_facts_are_pruned(db):
    engine, t = db
    ev = Events(engine, t, [Subscriber("a", ("x",), Recorder())], keep_done=__import__(
        "datetime").timedelta(0))  # fmt: skip
    ev.register()
    with engine.begin() as c:
        emit(c, t, "x")
    await ev.drain_once()
    with engine.begin() as c:
        c.execute(update(t.events).values(at=text("now() - interval '40 days'")))
    assert ev.prune() == 2  # the delivery, then the fact
    assert ev.backlog() == {"due": 0, "oldest_s": 0.0, "dead": 0}


def test_names_must_be_unique():
    with pytest.raises(ValueError):
        Events(
            None, None, [Subscriber("a", ("x",), Recorder()), Subscriber("a", ("y",), Recorder())]
        )
