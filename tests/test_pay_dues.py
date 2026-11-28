"""meydan.pay.dues: what an actor owes for the app's subject, its link, and what checkouts mean.

A real Postgres; the app's subject is an "order" kept in memory. Test data is fake.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from conftest import URL, unavailable
from sqlalchemy import MetaData, create_engine, insert, select, text

from meydan.bridge import connections as cx
from meydan.pay import checkout as co
from meydan.pay import dues as pay
from meydan.pay.checkout import Checkout, Status
from meydan.pay.dues import Dues, DueState, NotPayable, Payable
from meydan.runtime import define_tables
from meydan.vault import Keyring


class Orders:
    """The app's subject: an order is payable unless it's in `closed`; it records what it hears."""

    def __init__(self, now):
        self.now, self.closed, self.heard = now, set(), []

    def payable(self, conn, subject_id, actor_id):
        if subject_id in self.closed:
            return NotPayable("closed")
        return Payable(self.now() + timedelta(days=1), Decimal("50.00"), "EUR",
                       readable_until=self.now() + timedelta(days=10))  # fmt: skip

    def on_paid(self, conn, due, checkout):
        self.heard.append(("paid", due.subject_id, checkout.id))

    def on_released(self, conn, due, why):
        self.heard.append((str(why), due.subject_id))


class Clock:
    def __init__(self):
        self.t = datetime.now(UTC).replace(microsecond=0)

    def __call__(self):
        return self.t


@pytest.fixture
def world():
    engine = create_engine(URL)
    try:
        with engine.connect() as c:
            c.execute(text("select 1"))
    except Exception as e:
        unavailable(f"no Postgres test database: {e}")
    md = MetaData()
    t = define_tables(md)
    checkouts = co.define_table(md)
    table = pay.define_table(md)
    md.drop_all(engine)
    md.create_all(engine)
    with engine.begin() as c:
        actor = c.execute(insert(t.actors).values(kind="person").returning(t.actors.c.id)).scalar()
        cx.ensure(c, t, "shop-test")
    clock = Clock()
    orders = Orders(clock)
    kr = Keyring({"k1": Keyring.new_key()}, current="k1")
    dues = Dues(t, table, kr, {"order": orders}, now=clock)
    yield engine, t, checkouts, dues, orders, clock, actor
    md.drop_all(engine)
    engine.dispose()


def checkout(c, checkouts, due, actor, status: Status) -> Checkout:
    """A checkout of the due in `status`, recorded as meydan.pay.checkout would."""
    cid = uuid.uuid4()
    now = datetime.now(UTC)
    c.execute(insert(checkouts).values(
        id=cid, subject=due.checkout_subject, actor_id=actor, connection="shop-test",
        provider="fakepay", environment="test", status=status, watch_until=now + timedelta(days=1),
    ))  # fmt: skip
    return Checkout(
        id=cid, subject=due.checkout_subject, actor_id=actor, connection="shop-test",
        provider="fakepay", environment="test", status=status, reason=None, error_code=None,
        session_id=None, url=None, amount=None, currency=None, expires_at=None,
        watch_until=now, checked_at=None, next_check_at=None, created_at=now,
    )  # fmt: skip


def test_opening_a_due_gives_one_link_that_comes_back_the_same(world):
    engine, t, _, dues, _, clock, actor = world
    with engine.begin() as c:
        due, token = dues.open(c, "order:7", actor)
        assert (due.state, due.amount, due.currency, due.kind) == (
            DueState.OPEN,
            50,
            "EUR",
            "order",
        )
        again, same = dues.open(c, "order:7", actor)  # asked again: the same due and link
        assert again.id == due.id and same == token
        assert dues.link(c, due.id) == token
        assert dues.resolve(c, token).id == due.id
        assert dues.resolve(c, token + "x") is None
        stored = c.scalar(select(dues.table.c.link_sealed))
        assert token not in stored  # sealed, never readable in the table
        assert c.scalar(select(t.audit.c.action)) == "due:open:order"


def test_the_app_decides_whether_it_can_be_paid(world):
    engine, _, _, dues, orders, _, actor = world
    orders.closed.add("8")
    with engine.begin() as c:
        assert dues.open(c, "order:8", actor) == NotPayable("closed")
        with pytest.raises(pay.DueError):
            dues.open(c, "ticket:1", actor)  # no handler for that kind


def test_paid_once_however_often_it_is_reported_and_readable_until_the_apps_date(world):
    engine, _, checkouts, dues, orders, clock, actor = world
    handle = dues.checkout_handler(checkouts)
    with engine.begin() as c:
        due, token = dues.open(c, "order:9", actor)
        attempt = checkout(c, checkouts, due, actor, Status.PAID)
        handle(c, attempt, Status.PENDING)
        handle(c, attempt, Status.PENDING)  # the browser and the poll both report it
        assert orders.heard == [("paid", "9", attempt.id)]
        paid = dues.get(c, due.id)
        assert paid.state == DueState.PAID and paid.paid_at is not None
        clock.t += timedelta(days=3)  # past the payment deadline
        assert dues.resolve(c, token).state == DueState.PAID  # their own "paid" page
        clock.t += timedelta(days=10)  # past readable_until
        assert dues.resolve(c, token) is None


def test_a_failed_attempt_leaves_the_due_open_for_another(world):
    engine, _, checkouts, dues, orders, _, actor = world
    handle = dues.checkout_handler(checkouts)
    with engine.begin() as c:
        due, token = dues.open(c, "order:10", actor)
        handle(c, checkout(c, checkouts, due, actor, Status.FAILED), Status.PENDING)
        assert dues.get(c, due.id).state == DueState.OPEN and orders.heard == []
        assert dues.resolve(c, token).id == due.id


def test_a_refund_releases_unless_another_attempt_is_paid(world):
    engine, _, checkouts, dues, orders, _, actor = world
    handle = dues.checkout_handler(checkouts)
    with engine.begin() as c:
        due, _ = dues.open(c, "order:11", actor)
        first = checkout(c, checkouts, due, actor, Status.PAID)
        handle(c, first, Status.PENDING)
        second = checkout(c, checkouts, due, actor, Status.PAID)  # paid twice by mistake
        handle(c, second, Status.PENDING)
        c.execute(checkouts.update().where(checkouts.c.id == first.id).values(status="refunded"))
        handle(c, checkout_as(first, Status.REFUNDED), Status.PAID)
        assert dues.get(c, due.id).state == DueState.PAID  # the second still pays it
        c.execute(checkouts.update().where(checkouts.c.id == second.id).values(status="refunded"))
        handle(c, checkout_as(second, Status.REFUNDED), Status.PAID)
        assert dues.get(c, due.id).state == DueState.REFUNDED
        assert orders.heard[-1] == ("refunded", "11")


def checkout_as(c: Checkout, status: Status) -> Checkout:
    from dataclasses import replace

    return replace(c, status=status)


def test_past_the_deadline_it_expires_once_and_a_late_payment_still_counts(world):
    engine, _, checkouts, dues, orders, clock, actor = world
    with engine.begin() as c:
        due, token = dues.open(c, "order:12", actor)
        clock.t += timedelta(days=2)
        assert dues.resolve(c, token) is None  # the link no longer pays
        assert dues.expire(c) == 1 and dues.expire(c) == 0
        assert orders.heard == [("expired", "12")]
        late = checkout(c, checkouts, due, actor, Status.PAID)  # the provider took it anyway
        dues.checkout_handler(checkouts)(c, late, Status.PENDING)
        assert dues.get(c, due.id).state == DueState.PAID
        assert orders.heard[-1] == ("paid", "12", late.id)


def test_cancelling_closes_the_link_and_a_new_due_gets_a_new_one(world):
    engine, _, _, dues, orders, _, actor = world
    with engine.begin() as c:
        due, token = dues.open(c, "order:13", actor)
        assert dues.cancel(c, due.id).state == DueState.CANCELLED
        assert dues.cancel(c, due.id) is None  # once
        assert dues.resolve(c, token) is None
        assert orders.heard == [("cancelled", "13")]
        fresh, new_token = dues.open(c, "order:13", actor)
        assert fresh.id != due.id and new_token != token


def test_an_apps_own_link_kind_is_kept(world):
    engine, t, _, dues, _, _, actor = world
    mine = Dues(t, dues.table, dues.keyring, dues.subjects, kind="payment", now=dues.now)
    with engine.begin() as c:
        due, token = mine.open(c, "order:14", actor)
        assert c.scalar(select(t.expectations.c.kind)) == "payment"
        assert mine.resolve(c, token).id == due.id
        assert dues.resolve(c, token) is None  # another kind: not ours


def test_settled_outside_a_checkout_by_an_organization_once(world):
    """An organization covers it (paid outside our checkouts): the app fulfils it as paid, the
    link stops taking payments, and a payment that comes anyway still counts."""
    engine, t, checkouts, dues, orders, clock, actor = world
    with engine.begin() as c:
        org = c.execute(insert(t.actors).values(kind="org").returning(t.actors.c.id)).scalar()
        staff = c.execute(insert(t.actors).values(kind="person").returning(t.actors.c.id)).scalar()
        due, token = dues.open(c, "order:9", actor)
        assert due.payer == actor  # nobody else pays it
        with pytest.raises(pay.DueError):  # this app's orders have no on_settled
            dues.settle(c, due.id, by=org, note="covered by the partner")
    orders.on_settled = lambda conn, d: orders.heard.append(("settled", d.subject_id))
    with engine.begin() as c:
        done = dues.settle(c, due.id, by=org, note="covered by the partner", who=staff)
        assert done.state == DueState.SETTLED and done.settled_by == org and done.paid_at
        assert dues.settle(c, due.id, by=org, note="again") is None  # once
        assert dues.resolve(c, token).id == due.id  # opens as paid until readable_until
        actions = c.scalars(select(t.audit.c.action)).all()
        assert "due:settle:order" in actions and "due:settled:order" in actions
        late = checkout(c, checkouts, due, actor, Status.PAID)
        dues.checkout_handler(checkouts)(c, late, Status.PENDING)
        assert dues.get(c, due.id).state == DueState.PAID  # paid anyway: the payment counts
    assert orders.heard == [("settled", "9"), ("paid", "9", late.id)]


def test_someone_else_pays_it_when_a_payer_is_set(world):
    engine, t, _, dues, _, _, actor = world
    with engine.begin() as c:
        friend = c.execute(insert(t.actors).values(kind="person").returning(t.actors.c.id)).scalar()
        due, _ = dues.open(c, "order:10", actor)
        c.execute(dues.table.update().where(dues.table.c.id == due.id).values(payer_id=friend))
        again = dues.get(c, due.id)
    assert again.actor_id == actor and again.payer == friend
