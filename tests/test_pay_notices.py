"""meydan.pay.notices: a due's facts become messages, once, after the agent's reply.

A real Postgres; the runtime's own tables, facts and outbox; nothing is sent (the outbox is what we
read). Test data is fake.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from conftest import URL, msg, receive, runtime, unavailable
from sqlalchemy import MetaData, create_engine, func, select, text, update

from meydan.channels.base import Outbound
from meydan.events import Events
from meydan.pay import checkout as co
from meydan.pay import dues as pay
from meydan.pay.dues import Dues, NotPayable, Payable
from meydan.pay.notices import Notice, Notices, before_deadline, opened
from meydan.runtime import define_tables
from meydan.vault import Keyring


class Orders:
    def __init__(self):
        self.closed = set()

    def payable(self, conn, subject_id, actor_id):
        if subject_id in self.closed:
            return NotPayable("closed")
        return Payable(datetime.now(UTC) + timedelta(days=3), Decimal("50.00"), "EUR")

    def on_paid(self, conn, due, checkout):
        pass

    def on_released(self, conn, due, why):
        pass


class World:
    def __init__(self, engine, t, rt, dues):
        self.engine, self.t, self.rt, self.dues = engine, t, rt, dues
        self.windows: list[bool] = []

    def card(self, conn, due, where):
        self.windows.append(where.window_open)
        return [Outbound.text_message(f"Ödeme kartı {due.subject_id}")]

    def events(self, *notices: Notice, **kw) -> Events:
        n = Notices(self.engine, self.rt, self.dues, notices, wait_s=0, **kw)
        ev = Events(self.engine, self.t, n.subscribers())
        ev.register()
        return ev

    def writes(self, at: datetime, answered: bool = True):
        receive(self.rt, [msg(f"m{at.timestamp()}", "kaydolmak istiyorum", at)])
        with self.engine.begin() as c:
            if answered:  # the worker answered it: no turn running
                c.execute(update(self.t.inbox).values(processed_at=datetime.now(UTC)))
            sender = select(self.t.inbox.c.sender_identity_id).limit(1).scalar_subquery()
            cl = self.t.claims
            return c.scalar(select(cl.c.actor_id).where(cl.c.identity_id == sender))

    def open(self, actor, subject="order:1"):
        with self.engine.begin() as c:
            due, _ = self.dues.open(c, subject, actor)
        return due

    def sent(self):
        with self.engine.connect() as c:
            o = self.t.outbox
            return c.execute(select(o.c.ref, o.c.message).order_by(o.c.created_at)).all()


@pytest.fixture
def w():
    engine = create_engine(URL)
    try:
        with engine.connect() as c:
            c.execute(text("select 1"))
    except Exception as e:
        unavailable(f"no Postgres test database: {e}")
    md = MetaData()
    t = define_tables(md)
    co.define_table(md)
    table = pay.define_table(md)
    md.drop_all(engine)
    md.create_all(engine)

    async def handler(turn):
        return []

    rt = runtime((engine, t), handler)
    kr = Keyring({"k1": Keyring.new_key()}, current="k1")
    yield World(engine, t, rt, Dues(t, table, kr, {"order": Orders()}))
    md.drop_all(engine)
    engine.dispose()


async def test_the_pay_card_goes_once_when_no_turn_is_running(w):
    actor = w.writes(datetime.now(UTC))
    ev = w.events(opened(w.card))
    due = w.open(actor)
    assert await ev.drain_once() == 1
    ((ref, message),) = w.sent()
    assert ref == f"pay:{due.id}:opened" and message["text"] == "Ödeme kartı 1"
    assert w.windows == [True]
    with w.engine.begin() as c:  # the same fact delivered again sends nothing more
        c.execute(update(w.t.event_deliveries)
                  .values(status="pending", done_at=None, next_at=func.now()))  # fmt: skip
    await ev.drain_once()
    assert len(w.sent()) == 1


async def test_while_a_turn_runs_it_waits_and_follows_the_reply(w):
    actor = w.writes(datetime.now(UTC), answered=False)  # their message is not answered yet
    ev = w.events(opened(w.card))
    w.open(actor)
    await ev.drain_once()
    assert w.sent() == []  # it waits: the agent's reply comes first
    with w.engine.begin() as c:
        c.execute(update(w.t.inbox).values(processed_at=datetime.now(UTC)))
    await ev.drain_once()
    assert len(w.sent()) == 1


async def test_a_stuck_turn_holds_it_back_at_most_max_wait(w):
    actor = w.writes(datetime.now(UTC), answered=False)
    ev = w.events(opened(w.card), max_wait_s=0)
    w.open(actor)
    await ev.drain_once()
    assert len(w.sent()) == 1


async def test_a_pay_card_for_a_due_no_longer_open_is_not_sent(w):
    actor = w.writes(datetime.now(UTC))
    ev = w.events(opened(w.card))
    due = w.open(actor)
    with w.engine.begin() as c:
        w.dues.cancel(c, due.id)
    await ev.drain_once()
    assert w.sent() == []


async def test_a_reminder_waits_for_its_time_and_goes_only_if_still_open(w):
    actor = w.writes(datetime.now(UTC))
    ev = w.events(before_deadline(w.card, ahead=timedelta(days=1)))
    w.open(actor)
    await ev.drain_once()
    assert w.sent() == []  # two days early
    with w.engine.begin() as c:  # its time came
        c.execute(update(w.t.event_deliveries).values(next_at=func.now()))
        c.execute(update(w.dues.table).values(deadline=datetime.now(UTC) + timedelta(hours=1)))
    await ev.drain_once()
    ((ref, _),) = w.sent()
    assert ref.endswith(":reminder")


async def test_outside_the_window_the_card_is_told_so(w):
    actor = w.writes(datetime.now(UTC))
    with w.engine.begin() as c:  # their last message was two days ago
        c.execute(update(w.t.inbox).values(received_at=datetime.now(UTC) - timedelta(days=2)))
    ev = w.events(opened(w.card))
    w.open(actor)
    await ev.drain_once()
    assert w.windows == [False]  # the app answers with a template then


async def test_someone_who_never_wrote_gets_nothing(w):
    from sqlalchemy import insert

    with w.engine.begin() as c:
        actor = c.scalar(insert(w.t.actors).values(kind="person").returning(w.t.actors.c.id))
    ev = w.events(opened(w.card))
    w.open(actor)
    await ev.drain_once()
    assert w.sent() == []


async def test_notices_bound_after_the_runtime_was_built(w):
    actor = w.writes(datetime.now(UTC))
    n = Notices(w.engine, None, None, [opened(w.card)], wait_s=0)
    ev = Events(w.engine, w.t, n.subscribers())
    ev.register()
    w.open(actor)
    await ev.drain_once()
    assert w.sent() == []  # not bound yet: it waits
    n.bind(w.rt, w.dues)
    await ev.drain_once()
    assert len(w.sent()) == 1
