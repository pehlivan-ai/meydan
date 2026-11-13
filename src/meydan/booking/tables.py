"""Booking tables: staff, services, shifts, closures, bookings (with holds), a waitlist and
offers.

A closure is its own thing, not a booking with nobody in it: it has no customer, no price and
no place in the counts. Free time is shifts minus bookings minus closures.

The database guarantees what matters most: one member of staff is never booked twice at the
same time. Holds and confirmed bookings both block time (exclusion constraint); a hold that
runs out is moved to `expired` by `expire_holds`, which frees the time again.
Needs the btree_gist extension.
"""

from dataclasses import dataclass

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint

BOOKING_STATUSES = ("held", "confirmed", "cancelled", "expired", "attended", "no_show")
OFFER_OUTCOMES = ("pending", "accepted", "declined", "expired", "withdrawn", "missed")


@dataclass(frozen=True)
class BookingTables:
    staff: Table
    services: Table
    staff_services: Table
    shifts: Table
    customers: Table
    bookings: Table
    waitlist: Table
    offers: Table
    spaces: Table
    closures: Table


def define_tables(md: MetaData, prefix: str = "bk_") -> BookingTables:
    def ref(table: str) -> ForeignKey:
        return ForeignKey(f"{prefix}{table}.id")

    staff = Table(
        f"{prefix}staff", md, Column("id", Integer, primary_key=True), Column("name", String(80))
    )
    services = Table(
        f"{prefix}services",
        md,
        Column("id", Integer, primary_key=True),
        Column("name", String(80), nullable=False),
        Column("minutes", Integer, nullable=False),
        Column("price", Integer, nullable=False),  # in the business's currency, whole units
        # The kind of space it occupies (a chair, a basin); None: it needs no space of its own.
        Column("space_kind", String(40)),
    )
    spaces = Table(  # a physical place one booking occupies at a time
        f"{prefix}spaces",
        md,
        Column("id", Integer, primary_key=True),
        Column("name", String(80), nullable=False),
        Column("kind", String(40), nullable=False),
    )
    staff_services = Table(
        f"{prefix}staff_services",
        md,
        Column("staff_id", Integer, ref("staff"), primary_key=True),
        Column("service_id", Integer, ref("services"), primary_key=True),
    )
    shifts = Table(
        f"{prefix}shifts",
        md,
        Column("id", Integer, primary_key=True),
        Column("staff_id", Integer, ref("staff"), nullable=False),
        Column("during", TSTZRANGE, nullable=False),
    )
    customers = Table(
        f"{prefix}customers",
        md,
        Column("id", Integer, primary_key=True),
        Column("name", String(80), nullable=False),
        Column("reliability", Float, nullable=False, server_default="1"),  # attended / booked
    )
    bookings = Table(
        f"{prefix}bookings",
        md,
        Column("id", Integer, primary_key=True),
        Column("staff_id", Integer, ref("staff"), nullable=False),
        Column("service_id", Integer, ref("services"), nullable=False),
        Column("customer_id", Integer, ref("customers"), nullable=False),
        Column("space_id", Integer, ref("spaces")),  # None when the service needs no space
        Column("during", TSTZRANGE, nullable=False),
        Column("status", String(12), nullable=False),
        Column("hold_expires_at", DateTime(timezone=True)),
        Column("source", String(12), nullable=False, server_default="direct"),  # direct | fill
        Column("price", Integer, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        CheckConstraint(f"status IN {BOOKING_STATUSES}", name=f"{prefix}booking_status"),
        ExcludeConstraint(
            ("staff_id", "="),
            ("during", "&&"),
            where=text("status IN ('held', 'confirmed')"),
            name=f"{prefix}no_double_booking",
        ),
        ExcludeConstraint(  # a NULL space never conflicts: `=` on NULL is not true
            ("space_id", "="),
            ("during", "&&"),
            where=text("status IN ('held', 'confirmed')"),
            name=f"{prefix}no_double_space",
        ),
    )
    waitlist = Table(  # an agreed "tell me if a spot opens": service, optional staff, window
        f"{prefix}waitlist",
        md,
        Column("id", Integer, primary_key=True),
        Column("customer_id", Integer, ref("customers"), nullable=False),
        Column("service_id", Integer, ref("services"), nullable=False),
        Column("staff_id", Integer, ref("staff")),  # None: anyone who does the service
        Column("window", TSTZRANGE, nullable=False),  # when they could come
        # Agreed ground: "don't ask, just book it". The offer becomes a notice.
        Column("auto_accept", Boolean, nullable=False, server_default="false"),
        Column("active", Boolean, nullable=False, server_default="true"),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    offers = Table(
        f"{prefix}offers",
        md,
        Column("id", Integer, primary_key=True),
        Column("waitlist_id", Integer, ref("waitlist"), nullable=False),
        # The hold behind it; None when several people are asked at once (first yes books).
        Column("booking_id", Integer, ref("bookings")),
        Column("freed_booking_id", Integer, ref("bookings"), nullable=False),  # what opened up
        Column("staff_id", Integer, ref("staff"), nullable=False),
        Column("service_id", Integer, ref("services"), nullable=False),
        Column("customer_id", Integer, ref("customers"), nullable=False),
        Column("during", TSTZRANGE, nullable=False),  # the time offered
        Column("sent_at", DateTime(timezone=True), nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        Column("outcome", String(10), nullable=False, server_default="pending"),
        Column("answered_at", DateTime(timezone=True)),
        CheckConstraint(f"outcome IN {OFFER_OUTCOMES}", name=f"{prefix}offer_outcome"),
    )
    closures = Table(  # time nobody can be booked into: a day off, a member away for two hours
        f"{prefix}closures",
        md,
        Column("id", Integer, primary_key=True),
        Column("staff_id", Integer, ref("staff")),  # None: the whole business
        Column("during", TSTZRANGE, nullable=False),  # to the minute, not to the day
        Column("reason", String(200), nullable=False, server_default=""),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    return BookingTables(
        staff, services, staff_services, shifts, customers, bookings, waitlist, offers, spaces,
        closures,
    )  # fmt: skip
