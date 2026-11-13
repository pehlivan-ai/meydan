from datetime import UTC, datetime, timedelta

import pytest
from conftest import URL, unavailable
from psycopg.types.range import Range
from sqlalchemy import MetaData, create_engine, insert, select, text
from sqlalchemy.exc import OperationalError

from meydan.booking import core
from meydan.booking.tables import define_tables

DAY = datetime(2026, 11, 3, tzinfo=UTC)  # a Tuesday; times below are UTC for clarity


def at(h: int, m: int = 0) -> datetime:
    return DAY + timedelta(hours=h, minutes=m)


@pytest.fixture
def business():
    engine = create_engine(URL)
    try:
        engine.connect().close()
    except OperationalError as e:
        unavailable(f"no Postgres test database: {e}")
    with engine.begin() as c:
        # the no-overlap constraint needs this extension
        c.execute(text("create extension if not exists btree_gist"))
    md = MetaData()
    t = define_tables(md, prefix="bkt_")
    md.drop_all(engine)
    md.create_all(engine)
    with engine.begin() as c:
        c.execute(insert(t.staff), [{"id": 1, "name": "Ece"}])
        c.execute(
            insert(t.services),
            [
                {"id": 1, "name": "cut", "minutes": 45, "price": 900},
                {"id": 2, "name": "colour", "minutes": 120, "price": 2400},
                {"id": 3, "name": "brows", "minutes": 30, "price": 400},
            ],
        )
        c.execute(insert(t.staff_services), [{"staff_id": 1, "service_id": i} for i in (1, 2, 3)])
        c.execute(insert(t.shifts).values(staff_id=1, during=Range(at(9), at(18))))
        c.execute(
            insert(t.customers),
            [{"id": i, "name": n} for i, n in enumerate(["Ada", "Bora", "Can", "Deniz"], 1)],
        )
    yield engine, t
    md.drop_all(engine)
    engine.dispose()


def wait(c, t, customer_id, service_id, lo, hi, asked_at):
    c.execute(
        insert(t.waitlist).values(
            customer_id=customer_id,
            service_id=service_id,
            window=Range(lo, hi),
            created_at=asked_at,
        )
    )


def test_staff_is_never_double_booked(business):
    engine, t = business
    with engine.begin() as c:
        first = core.book(c, t, staff_id=1, service_id=1, customer_id=1, start=at(10), now=at(8))
        clash = core.book(
            c, t, staff_id=1, service_id=3, customer_id=2, start=at(10, 30), now=at(8)
        )
        after = core.book(
            c, t, staff_id=1, service_id=3, customer_id=2, start=at(10, 45), now=at(8)
        )
    assert first and after and clash is None


def test_freed_colour_slot_goes_to_who_asked_first_then_next_on_decline(business):
    engine, t = business
    with engine.begin() as c:
        colour = core.book(c, t, staff_id=1, service_id=2, customer_id=1, start=at(14), now=at(7))
        wait(c, t, 2, 1, at(12), at(18), asked_at=at(6))  # Bora asked first
        wait(c, t, 3, 1, at(12), at(18), asked_at=at(7))
        core.cancel(c, t, colour)
        offer = core.offer_next(c, t, colour, now=at(9))
        assert (offer.customer, offer.start) == ("Bora", at(14))
        assert not core.answer(c, t, offer.id, accept=False, now=at(9, 5))
        offer = core.offer_next(c, t, colour, now=at(9, 5))
        assert offer.customer == "Can"
        assert core.answer(c, t, offer.id, accept=True, now=at(9, 10))
        assert [f["price"] for f in core.fills(c, t)] == [900]


def test_one_long_gap_is_filled_by_several_short_services(business):
    engine, t = business
    with engine.begin() as c:
        colour = core.book(c, t, staff_id=1, service_id=2, customer_id=1, start=at(14), now=at(7))
        wait(c, t, 2, 1, at(14), at(16), asked_at=at(6))
        wait(c, t, 3, 3, at(14), at(16), asked_at=at(6, 30))
        core.cancel(c, t, colour)
        for _ in range(2):
            offer = core.offer_next(c, t, colour, now=at(9))
            core.answer(c, t, offer.id, accept=True, now=at(9, 1))
        assert core.offer_next(c, t, colour, now=at(9, 2)) is None  # nobody else waits
        assert sorted(f["price"] for f in core.fills(c, t)) == [400, 900]


def test_unanswered_offer_expires_frees_the_time_and_moves_on(business):
    engine, t = business
    with engine.begin() as c:
        cut = core.book(c, t, staff_id=1, service_id=1, customer_id=1, start=at(15), now=at(7))
        wait(c, t, 2, 1, at(9), at(18), asked_at=at(6))
        wait(c, t, 3, 1, at(9), at(18), asked_at=at(7))
        core.cancel(c, t, cut)
        silent = core.offer_next(c, t, cut, now=at(10), hold_for=timedelta(minutes=10))
        assert core.expire_holds(c, t, now=at(10, 10)) == [cut]
        assert not core.answer(c, t, silent.id, accept=True, now=at(10, 11))  # too late
        offer = core.offer_next(c, t, cut, now=at(10, 10))
        assert offer.customer == "Can" and offer.start == at(15)


def test_no_offer_too_close_to_the_slot(business):
    engine, t = business
    with engine.begin() as c:
        cut = core.book(c, t, staff_id=1, service_id=1, customer_id=1, start=at(10), now=at(7))
        wait(c, t, 2, 1, at(9), at(18), asked_at=at(6))
        core.cancel(c, t, cut)
        assert core.offer_next(c, t, cut, now=at(9, 50)) is None  # less than the lead time
        status = c.scalar(select(t.bookings.c.status).where(t.bookings.c.id == cut))
    assert status == "cancelled"


def test_asking_several_at_once_first_yes_books_and_others_are_withdrawn(business):
    engine, t = business
    with engine.begin() as c:
        cut = core.book(c, t, staff_id=1, service_id=1, customer_id=1, start=at(15), now=at(7))
        for who, asked in ((2, 6), (3, 7), (4, 8)):
            wait(c, t, who, 1, at(9), at(18), asked_at=at(asked))
        core.cancel(c, t, cut)
        offers = [core.offer_next(c, t, cut, now=at(10), hold=False) for _ in range(3)]
        assert [o.customer for o in offers] == ["Bora", "Can", "Deniz"]
        assert {o.start for o in offers} == {at(15)}  # the same time, nothing held
        assert core.answer(c, t, offers[1].id, accept=True, now=at(10, 3))  # Can is quickest
        assert not core.answer(c, t, offers[0].id, accept=True, now=at(10, 4))  # withdrawn
        outcomes = c.scalars(select(t.offers.c.outcome).order_by(t.offers.c.id)).all()
    assert outcomes == ["withdrawn", "accepted", "withdrawn"]


def test_agreed_ground_books_without_asking(business):
    engine, t = business
    with engine.begin() as c:
        cut = core.book(c, t, staff_id=1, service_id=1, customer_id=1, start=at(15), now=at(7))
        wait(c, t, 2, 1, at(9), at(18), asked_at=at(6))
        c.execute(
            insert(t.waitlist).values(  # Can said "if a cut opens, just book me"
                customer_id=3,
                service_id=1,
                window=Range(at(9), at(18)),
                created_at=at(5),
                auto_accept=True,
            )
        )
        core.cancel(c, t, cut)
        notice = core.offer_next(c, t, cut, now=at(10))
        assert notice.auto and notice.customer == "Can"
        assert [f["price"] for f in core.fills(c, t)] == [900]


def test_policy_quiet_hours_and_styles():
    from meydan.booking.policy import OfferPolicy

    p = OfferPolicy()
    tz = DAY.tzinfo
    assert p.quiet_until(datetime(2026, 11, 3, 7, 30, tzinfo=tz)) == datetime(
        2026, 11, 3, 9, tzinfo=tz
    )
    assert p.quiet_until(datetime(2026, 11, 3, 22, tzinfo=tz)) == datetime(
        2026, 11, 4, 9, tzinfo=tz
    )
    assert p.quiet_until(datetime(2026, 11, 3, 12, tzinfo=tz)) is None
    assert OfferPolicy.of("fast").fanout == 3 and not OfferPolicy.of("fast").hold
    assert OfferPolicy.of("fair").with_(lead_min=20, fanout=None).lead_min == 20


def with_selin_and_basins(c, t, basins: int) -> None:
    """A second member of staff who does everything, and cut + colour now need a basin."""
    c.execute(insert(t.staff), [{"id": 2, "name": "Selin"}])
    c.execute(insert(t.staff_services), [{"staff_id": 2, "service_id": i} for i in (1, 2, 3)])
    c.execute(insert(t.shifts).values(staff_id=2, during=Range(at(9), at(18))))
    c.execute(
        insert(t.spaces), [{"name": f"basin {i + 1}", "kind": "basin"} for i in range(basins)]
    )
    c.execute(t.services.update().where(t.services.c.id.in_((1, 2))).values(space_kind="basin"))


def test_a_service_that_needs_a_space_waits_for_one_even_when_staff_is_free(business):
    engine, t = business
    with engine.begin() as c:
        with_selin_and_basins(c, t, basins=1)
        first = core.book(c, t, staff_id=1, service_id=1, customer_id=1, start=at(10), now=at(8))
        no_basin = core.book(
            c, t, staff_id=2, service_id=1, customer_id=2, start=at(10, 15), now=at(8)
        )
        no_space_needed = core.book(
            c, t, staff_id=2, service_id=3, customer_id=2, start=at(10, 15), now=at(8)
        )
        c.execute(insert(t.spaces).values(name="basin 2", kind="basin"))
        ece = core.book(c, t, staff_id=1, service_id=1, customer_id=3, start=at(12), now=at(8))
        selin = core.book(c, t, staff_id=2, service_id=1, customer_id=4, start=at(12), now=at(8))
        used = c.scalars(
            select(t.bookings.c.space_id).where(t.bookings.c.id.in_((ece, selin)))
        ).all()
    assert first and no_basin is None and no_space_needed
    assert ece and selin and len(set(used)) == 2  # two basins, one booking each


def test_a_freed_time_skips_whoever_has_no_space_for_it(business):
    engine, t = business
    with engine.begin() as c:
        with_selin_and_basins(c, t, basins=1)
        core.book(c, t, staff_id=2, service_id=1, customer_id=1, start=at(10), now=at(8))  # basin
        freed = core.book(c, t, staff_id=1, service_id=3, customer_id=4, start=at(10), now=at(8))
        wait(c, t, 2, 1, at(9), at(18), at(7))  # Bora asked first, for a cut: needs the basin
        wait(c, t, 3, 3, at(9), at(18), at(7, 30))  # Can, brows: no space needed
        core.cancel(c, t, freed)
        offer = core.offer_next(c, t, freed, at(8))
    assert offer.customer == "Can" and offer.start == at(10)
