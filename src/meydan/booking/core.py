"""Booking and filling: hold, confirm, cancel, and offer freed time to the waitlist in order.

Every function takes `now` explicitly instead of reading a clock, so a day (or a month) can be
simulated in seconds and tests are exact. Run each call inside the caller's transaction.

Filling a freed booking:
  cancel → offer_next (hold the best fitting gap for the best candidate, send an offer)
         → answer (accept: confirm as a fill; decline: free it) or expire_holds (no answer)
         → offer_next again, until nobody fits the remaining gaps.

Two variations, chosen by the caller (see `policy.OfferPolicy`):
  - `hold=False`: nothing is held, so several people can be asked for the same time; the first
    yes books it and the others' overlapping offers are withdrawn.
  - a waitlist entry with `auto_accept` (agreed ground) is booked at once; the offer is a notice.
"""

import math
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg.types.range import Range
from sqlalchemy import Connection, and_, exc, func, insert, select, update

from meydan.booking.tables import BookingTables

GRID = timedelta(minutes=15)


@dataclass(frozen=True)
class Offer:
    id: int
    customer_id: int
    customer: str
    service: str
    staff: str
    start: datetime
    end: datetime
    expires_at: datetime
    price: int
    auto: bool  # booked already under agreed ground; the message is a notice, not a question


def _ceil(dt: datetime, step: timedelta = GRID) -> datetime:
    epoch = datetime(2000, 1, 1, tzinfo=dt.tzinfo)
    return epoch + step * math.ceil((dt - epoch) / step)


def book(
    conn: Connection,
    t: BookingTables,
    *,
    staff_id: int,
    service_id: int,
    customer_id: int,
    start: datetime,
    now: datetime,
    hold_for: timedelta | None = None,
    source: str = "direct",
) -> int | None:
    """Confirm (or, with `hold_for`, hold) a booking. None if the time is taken: the member of
    staff is busy, or the service needs a kind of space and none of that kind is free."""
    sv = t.services
    minutes, price, kind = conn.execute(
        select(sv.c.minutes, sv.c.price, sv.c.space_kind).where(sv.c.id == service_id)
    ).one()
    during = Range(start, start + timedelta(minutes=minutes))
    row = {
        "staff_id": staff_id,
        "service_id": service_id,
        "customer_id": customer_id,
        "during": during,
        "status": "held" if hold_for else "confirmed",
        "hold_expires_at": now + hold_for if hold_for else None,
        "source": source,
        "price": price,
        "created_at": now,
    }
    # Without a space: one try. With one: each free space of the kind, in order, until one holds
    # (someone else may take a space between our look and our insert).
    for space_id in [None] if kind is None else free_spaces(conn, t, kind, during):
        try:
            with conn.begin_nested():  # a conflict rolls back only this insert
                return conn.scalar(
                    insert(t.bookings)
                    .values(row | {"space_id": space_id})
                    .returning(t.bookings.c.id)
                )
        except exc.IntegrityError:
            continue
    return None


def free_spaces(conn: Connection, t: BookingTables, kind: str, during: Range) -> list[int]:
    """Spaces of one kind with no held or confirmed booking overlapping `during`, by id."""
    b, sp = t.bookings, t.spaces
    taken = select(b.c.space_id).where(
        b.c.space_id.is_not(None),
        b.c.status.in_(("held", "confirmed")),
        b.c.during.op("&&")(during),
    )
    return list(
        conn.scalars(
            select(sp.c.id).where(sp.c.kind == kind, sp.c.id.not_in(taken)).order_by(sp.c.id)
        )
    )


def cancel(conn: Connection, t: BookingTables, booking_id: int) -> None:
    b = t.bookings
    conn.execute(
        update(b)
        .where(b.c.id == booking_id, b.c.status.in_(("held", "confirmed")))
        .values(status="cancelled")
    )


def expire_holds(conn: Connection, t: BookingTables, now: datetime) -> list[int]:
    """Free holds whose time ran out; their offers (and unheld ones past their time) expire.
    Returns freed booking ids to refill."""
    b, o = t.bookings, t.offers
    held = conn.scalars(
        update(b)
        .where(b.c.status == "held", b.c.hold_expires_at <= now)
        .values(status="expired")
        .returning(b.c.id)
    ).all()
    late = o.c.booking_id.in_(held) | (o.c.booking_id.is_(None) & (o.c.expires_at <= now))
    return list(
        conn.scalars(
            update(o)
            .where(late, o.c.outcome == "pending")
            .values(outcome="expired", answered_at=now)
            .returning(o.c.freed_booking_id)
        ).all()
    )


def free_gaps(
    conn: Connection, t: BookingTables, staff_id: int, lo: datetime, hi: datetime
) -> list[tuple[datetime, datetime]]:
    """Free time of one member of staff within [lo, hi): shifts minus held/confirmed bookings
    and minus closures (theirs, or the whole business's)."""
    window = Range(lo, hi)
    shifts = conn.scalars(
        select(t.shifts.c.during).where(
            t.shifts.c.staff_id == staff_id, t.shifts.c.during.op("&&")(window)
        )
    ).all()
    busy = sorted(
        (r.lower, r.upper)
        for r in conn.scalars(
            select(t.bookings.c.during).where(
                t.bookings.c.staff_id == staff_id,
                t.bookings.c.status.in_(("held", "confirmed")),
                t.bookings.c.during.op("&&")(window),
            )
        )
    )
    c = t.closures
    busy = sorted(
        busy
        + [
            (r.lower, r.upper)
            for r in conn.scalars(
                select(c.c.during).where(
                    (c.c.staff_id.is_(None)) | (c.c.staff_id == staff_id),
                    c.c.during.op("&&")(window),
                )
            )
        ]
    )
    gaps = []
    for s in shifts:
        cur, end = max(s.lower, lo), min(s.upper, hi)
        for b_lo, b_hi in busy:
            if b_hi <= cur or b_lo >= end:
                continue
            if b_lo > cur:
                gaps.append((cur, b_lo))
            cur = max(cur, b_hi)
        if cur < end:
            gaps.append((cur, end))
    return gaps


def offer_next(
    conn: Connection,
    t: BookingTables,
    freed_booking_id: int,
    now: datetime,
    *,
    hold_for: timedelta = timedelta(minutes=15),
    lead: timedelta = timedelta(minutes=45),
    hold: bool = True,
    exclude_customers: Collection[int] = (),
) -> Offer | None:
    """Offer the best waitlisted fit for the time a cancelled booking freed, holding it unless
    `hold` is False. `hold_for` is how long the offer stays open either way."""
    b, w, o, sv, ss = t.bookings, t.waitlist, t.offers, t.services, t.staff_services
    staff_id, during = conn.execute(
        select(b.c.staff_id, b.c.during).where(b.c.id == freed_booking_id)
    ).one()
    # The freed time plus any free time touching it: a cancelled 30 minutes next to an empty
    # half hour can take a one-hour service.
    day_lo = max(during.lower.replace(hour=0, minute=0), _ceil(now + lead))
    gaps = [
        (lo, hi)
        for lo, hi in free_gaps(conn, t, staff_id, day_lo, during.upper + timedelta(days=1))
        if lo <= during.upper and hi >= during.lower and hi > lo
    ]
    if not gaps:
        return None
    asked = select(o.c.waitlist_id).where(o.c.freed_booking_id == freed_booking_id)
    busy_customers = select(o.c.waitlist_id).where(o.c.outcome == "pending")
    rows = conn.execute(
        select(
            w.c.id,
            w.c.customer_id,
            w.c.service_id,
            w.c.window,
            w.c.created_at,
            w.c.auto_accept,
            sv.c.minutes,
            t.customers.c.reliability,
        )
        .join(sv, sv.c.id == w.c.service_id)
        .join(ss, and_(ss.c.service_id == w.c.service_id, ss.c.staff_id == staff_id))
        .join(t.customers, t.customers.c.id == w.c.customer_id)
        .where(
            w.c.active,
            (w.c.staff_id.is_(None)) | (w.c.staff_id == staff_id),
            w.c.id.not_in(asked),
            w.c.id.not_in(busy_customers),
            w.c.customer_id.not_in(exclude_customers) if exclude_customers else True,
        )
    ).all()
    # Fairness first (who asked first), then reliability; the first gap that fits wins.
    for r in sorted(rows, key=lambda r: (r.created_at, -r.reliability)):
        need = timedelta(minutes=r.minutes)
        for g_lo, g_hi in gaps:
            # Must overlap the freed time (only that is a fill, and billable); start where it
            # opened if possible, else as early as still overlaps it.
            lo, hi = max(g_lo, r.window.lower), min(g_hi, r.window.upper)
            exact = _ceil(max(lo, during.lower))
            earliest = _ceil(max(lo, during.lower - need + GRID))
            start = exact if exact + need <= hi else earliest
            if start + need <= hi and start < during.upper and start + need > during.lower:
                booking_id = None
                if r.auto_accept or hold:
                    booking_id = book(
                        conn,
                        t,
                        staff_id=staff_id,
                        service_id=r.service_id,
                        customer_id=r.customer_id,
                        start=start,
                        now=now,
                        hold_for=None if r.auto_accept else hold_for,
                        source="fill",
                    )
                    if booking_id is None:
                        continue
                if r.auto_accept:
                    conn.execute(update(w).where(w.c.id == r.id).values(active=False))
                offer_id = conn.scalar(
                    insert(o)
                    .values(
                        waitlist_id=r.id,
                        booking_id=booking_id,
                        freed_booking_id=freed_booking_id,
                        staff_id=staff_id,
                        service_id=r.service_id,
                        customer_id=r.customer_id,
                        during=Range(start, start + need),
                        sent_at=now,
                        expires_at=now + hold_for,
                        outcome="accepted" if r.auto_accept else "pending",
                        answered_at=now if r.auto_accept else None,
                    )
                    .returning(o.c.id)
                )
                return _offer(conn, t, offer_id)
    return None


def answer(conn: Connection, t: BookingTables, offer_id: int, accept: bool, now: datetime) -> bool:
    """Apply a customer's answer. True if the spot is now theirs."""
    o, b, w = t.offers, t.bookings, t.waitlist
    offer = conn.execute(
        select(o).where(o.c.id == offer_id, o.c.outcome == "pending").with_for_update()
    ).first()
    if offer is None or offer.expires_at <= now:
        return False  # too late: already expired, answered or withdrawn
    if offer.booking_id is None:  # asked together with others: the first yes books it
        if not accept:
            conn.execute(
                update(o).where(o.c.id == offer_id).values(outcome="declined", answered_at=now)
            )
            return False
        booking_id = book(
            conn,
            t,
            staff_id=offer.staff_id,
            service_id=offer.service_id,
            customer_id=offer.customer_id,
            start=offer.during.lower,
            now=now,
            source="fill",
        )
        conn.execute(
            update(o)
            .where(o.c.id == offer_id)
            .values(
                outcome="accepted" if booking_id else "missed",
                answered_at=now,
                booking_id=booking_id,
            )
        )
        if booking_id is None:
            return False
        conn.execute(update(w).where(w.c.id == offer.waitlist_id).values(active=False))
        conn.execute(  # the time is gone for everyone else asked for it
            update(o)
            .where(
                o.c.freed_booking_id == offer.freed_booking_id,
                o.c.outcome == "pending",
                o.c.booking_id.is_(None),
                o.c.during.op("&&")(offer.during),
            )
            .values(outcome="withdrawn", answered_at=now)
        )
        return True
    conn.execute(
        update(o)
        .where(o.c.id == offer_id)
        .values(outcome="accepted" if accept else "declined", answered_at=now)
    )
    conn.execute(
        update(b)
        .where(b.c.id == offer.booking_id)
        .values(status="confirmed" if accept else "expired", hold_expires_at=None)
    )
    if accept:
        conn.execute(update(w).where(w.c.id == offer.waitlist_id).values(active=False))
    return accept


def fills(conn: Connection, t: BookingTables) -> list[dict]:
    """Bookings that exist only because a freed spot was offered and taken: the billable unit."""
    b, o = t.bookings, t.offers
    rows = conn.execute(
        select(b.c.id, b.c.price, b.c.during, o.c.freed_booking_id, o.c.sent_at, o.c.answered_at)
        .join(o, o.c.booking_id == b.c.id)
        .where(b.c.source == "fill", b.c.status.in_(("confirmed", "attended")))
        .order_by(o.c.answered_at)
    ).mappings()
    return [dict(r) for r in rows]


def _offer(conn: Connection, t: BookingTables, offer_id: int) -> Offer:
    o = t.offers
    r = conn.execute(
        select(
            o.c.id,
            o.c.customer_id,
            o.c.expires_at,
            o.c.during,
            o.c.outcome,
            t.services.c.price,
            t.customers.c.name.label("customer"),
            t.services.c.name.label("service"),
            t.staff.c.name.label("staff"),
        )
        .join(t.customers, t.customers.c.id == o.c.customer_id)
        .join(t.services, t.services.c.id == o.c.service_id)
        .join(t.staff, t.staff.c.id == o.c.staff_id)
        .where(o.c.id == offer_id)
    ).one()
    return Offer(
        r.id,
        r.customer_id,
        r.customer,
        r.service,
        r.staff,
        r.during.lower,
        r.during.upper,
        r.expires_at,
        r.price,
        auto=r.outcome == "accepted",
    )


def utilization(
    conn: Connection, t: BookingTables, lo: datetime, hi: datetime
) -> tuple[float, float]:
    """(booked minutes, shift minutes) in [lo, hi), for confirmed and attended bookings."""
    window = Range(lo, hi)

    def minutes(table, cond) -> float:
        r = func.coalesce(func.sum(func.upper(table.c.during) - func.lower(table.c.during)), None)
        total = conn.scalar(select(r).where(cond, table.c.during.op("<@")(window)))
        return total.total_seconds() / 60 if total else 0.0

    booked = minutes(t.bookings, t.bookings.c.status.in_(("confirmed", "attended")))
    shift = minutes(t.shifts, t.shifts.c.id.isnot(None))
    return booked, shift
