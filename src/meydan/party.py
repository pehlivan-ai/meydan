"""Parties: people in something together, each in a seat (docs/party.md).

Experimental. A **party** is the app's shared thing with places for people: a team of two, a match
of four, friends going to a concert together. A **seat** is one place in it: who sits there (or who
was asked to), in which role, and where they are (invited, accepted, declined, expired, left).
What a party is *for* stays the app's: its `kind` ("app.team") and `subject` ("tournament:<id>").
meydan keeps who is in, the invitations, the capacity and the facts; the app's **handler** says
what a seat taken or given up means (a registration opened, a booking released).

Someone joins through one of three doors to the same seat:

- **address**: we ask them at an address (`ask`: a contact we start, usually a template) or in a
  conversation they already have with us (`ask_in`). An expectation (meydan expect, kind `party`)
  carries the buttons; meydan checks the answer (token, the asking message, the address's owner,
  in time, once) and `handler()` takes the seat.
- **code**: the seat's short code, which the inviter forwards themselves (in a link with a
  prefilled message, say); whoever writes it back takes the seat (`redeem`). It is an invitation,
  not a secret: taking the seat is still the person's own step, and the app decides its cost.
- **claim**: the person says who invited them; the app finds the open seats that inviter made
  (`invited_by`) and asks them to confirm before `accept(door="claim")`. The weakest proof, so
  the facts carry the door and the app tells the inviter.

Capacity is data on the party (`Capacity`: exactly n, a range, or no ceiling). Invited and
accepted seats count against its ceiling; a party is `confirmed` once its accepted seats reach its
minimum. Each transition happens once (`UPDATE ... WHERE state = ...`).

Facts (meydan.events), in the change's transaction, `subject_id` the seat's id (the party's for
`party.*`): `seat.invited`, `seat.accepted`, `seat.declined`, `seat.expired`, `seat.left`,
`party.confirmed`; data: party_id, kind, subject, door.

Seams, documented and not built: a party with a time or topic is a *gathering*; the way there a
*plan*; and a person's *circle* (the people they know and trust) may grow from a party: each kind
says whether accepting adds people to each other's circle (`circle`: auto | ask | none).
"""

import logging
import re
import secrets
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Protocol

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

import meydan.runtime  # noqa: F401 (first: meydan.events and meydan.runtime import each other)
from meydan.channels.base import Address, Outbound
from meydan.events import emit
from meydan.ids import uuid7
from meydan.runtime import expect
from meydan.runtime.identity import Routed, find, owner, propose
from meydan.runtime.tables import RuntimeTables

log = logging.getLogger(__name__)

KIND = "party"  # the expectations behind an invite at an address
ACCEPT, DECLINE = "accept", "decline"
OPTIONS = (ACCEPT, DECLINE)
# A seat's code: 6 letters and digits without look-alikes (0/O, 1/I/L), shown as "#K7Q2MX".
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
CODE_MARK = "#"
_CODE = re.compile(rf"{CODE_MARK}([{CODE_ALPHABET}]{{{CODE_LENGTH}}})\b", re.IGNORECASE)


class PartyState(StrEnum):
    FORMING = "forming"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class SeatState(StrEnum):
    INVITED = "invited"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    LEFT = "left"


HELD = (SeatState.INVITED, SeatState.ACCEPTED)  # count against the ceiling


class Door(StrEnum):
    CREATED = "created"  # the party's creator, seated when it was made
    ADDRESS = "address"
    CODE = "code"
    CLAIM = "claim"


Circle = Literal["auto", "ask", "none"]  # a seam: whether accepting joins people's circles


@dataclass(frozen=True)
class Capacity:
    """How many seats: at least `min` accepted to be confirmed, at most `max` held (None: no
    ceiling)."""

    min: int = 1
    max: int | None = None

    @classmethod
    def exactly(cls, n: int) -> "Capacity":
        return cls(n, n)


@dataclass(frozen=True)
class Party:
    id: uuid.UUID
    kind: str
    subject: str | None
    state: PartyState
    capacity: Capacity
    created_by: uuid.UUID | None
    data: dict[str, Any]
    created_at: datetime

    @classmethod
    def of(cls, row) -> "Party":
        return cls(
            id=row.id, kind=row.kind, subject=row.subject, state=PartyState(row.state),
            capacity=Capacity(row.min_seats, row.max_seats), created_by=row.created_by,
            data=dict(row.data or {}), created_at=row.created_at,
        )  # fmt: skip


@dataclass(frozen=True)
class Seat:
    id: uuid.UUID
    party_id: uuid.UUID
    role: str
    state: SeatState
    actor_id: uuid.UUID | None
    name: str | None  # as the inviter gave it
    address: Address | None  # where we may ask them (as the inviter gave it)
    code: str | None
    invited_by: uuid.UUID | None
    door: Door | None  # how it was taken
    expectation_id: uuid.UUID | None  # the latest ask at an address
    expires_at: datetime | None
    answered_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, row) -> "Seat":
        address = Address(row.address_scheme, row.address_value) if row.address_value else None
        return cls(
            id=row.id, party_id=row.party_id, role=row.role, state=SeatState(row.state),
            actor_id=row.actor_id, name=row.name, address=address, code=row.code,
            invited_by=row.invited_by, door=Door(row.door) if row.door else None,
            expectation_id=row.expectation_id, expires_at=row.expires_at,
            answered_at=row.answered_at, created_at=row.created_at,
        )  # fmt: skip


@dataclass(frozen=True)
class Outcome:
    """What a change of a seat came to: the party, the seat, and what the kind's handler says back
    (sent when the change came from a pressed button)."""

    party: Party
    seat: Seat
    replies: tuple[Outbound, ...] = ()


class Handler(Protocol):
    """The app's meaning of one kind of party. Each runs in the change's transaction."""

    def on_accepted(
        self, conn: Connection, party: Party, seat: Seat, door: Door
    ) -> Sequence[Outbound]:
        """A seat was taken: do what it means (open a registration). Returns what to say back
        when it was taken by pressing a button (the other doors answer in their own turn)."""
        ...

    def on_released(self, conn: Connection, party: Party, seat: Seat) -> Sequence[Outbound]:
        """An invited seat was declined or expired, or a taken one left: free what was held.
        Returns what to say back when it was declined by pressing a button."""
        ...


@dataclass(frozen=True)
class Kind:
    """One kind of party, as the app defines it."""

    name: str  # "app.team": the app's name first, so kinds of different apps never clash
    capacity: Capacity
    handler: Handler | None = None
    invite_ttl: timedelta = timedelta(days=3)
    circle: Circle = "none"  # documented, not built yet (docs/party.md)


@dataclass(frozen=True)
class PartyTables:
    parties: Table
    seats: Table


def define_tables(
    metadata: MetaData, *, prefix: str = "meydan_", name: str = "parties"
) -> PartyTables:
    """The parties and their seats, on the app's metadata (`name`, `<name>_seats`); they refer to
    meydan's runtime tables (actors, expectations), which carry `prefix`."""
    actors = f"{prefix}actors.id"
    party_states = ", ".join(repr(str(s)) for s in PartyState)
    seat_states = ", ".join(repr(str(s)) for s in SeatState)
    doors = ", ".join(repr(str(d)) for d in Door)

    def stamps() -> list[Column]:
        return [
            Column("created_at", DateTime(timezone=True), server_default=func.now(),
                   nullable=False),
            Column("updated_at", DateTime(timezone=True), server_default=func.now(),
                   onupdate=func.now(), nullable=False),
        ]  # fmt: skip

    parties = Table(
        name,
        metadata,
        Column("id", UUID(as_uuid=True), primary_key=True, default=uuid7),
        Column("kind", Text, nullable=False),
        Column("subject", Text),  # the app's: "tournament:<uuid>"; null: a party of its own
        Column("state", Text, nullable=False, server_default=PartyState.FORMING.value),
        Column("min_seats", Integer, nullable=False),
        Column("max_seats", Integer),  # null: no ceiling
        Column("created_by", UUID(as_uuid=True), ForeignKey(actors)),
        Column("data", JSONB, nullable=False, server_default="{}"),
        *stamps(),
        CheckConstraint(f"state IN ({party_states})", name="state"),
        CheckConstraint("length(kind) BETWEEN 1 AND 100", name="kind"),
        CheckConstraint(
            "min_seats >= 1 AND (max_seats IS NULL OR max_seats >= min_seats)", name="capacity"
        ),  # fmt: skip
    )
    Index(f"ix_{name}_subject", parties.c.kind, parties.c.subject)
    seats = Table(
        f"{name}_seats",
        metadata,
        Column("id", UUID(as_uuid=True), primary_key=True, default=uuid7),
        Column("party_id", UUID(as_uuid=True), ForeignKey(f"{name}.id"), nullable=False),
        Column("role", Text, nullable=False),
        Column("state", Text, nullable=False),
        Column("actor_id", UUID(as_uuid=True), ForeignKey(actors)),  # null until known
        Column("name", Text),
        Column("address_scheme", Text),
        Column("address_value", Text),
        Column("code", Text, unique=True),
        Column("invited_by", UUID(as_uuid=True), ForeignKey(actors)),
        Column("door", Text),
        Column("expectation_id", UUID(as_uuid=True), ForeignKey(f"{prefix}expectations.id")),
        Column("expires_at", DateTime(timezone=True)),
        Column("answered_at", DateTime(timezone=True)),
        *stamps(),
        CheckConstraint(f"state IN ({seat_states})", name="state"),
        CheckConstraint(f"door IS NULL OR door IN ({doors})", name="door"),
        CheckConstraint("length(role) BETWEEN 1 AND 50", name="role"),
    )
    Index(f"ix_{name}_seats_party", seats.c.party_id)
    Index(
        f"ix_{name}_seats_actor", seats.c.actor_id, postgresql_where=seats.c.actor_id.is_not(None)
    )
    Index(f"ix_{name}_seats_invited_by", seats.c.invited_by,
          postgresql_where=seats.c.state == SeatState.INVITED.value)  # fmt: skip
    return PartyTables(parties, seats)


class SeatError(Exception):
    """Why a seat couldn't be given or taken: full, closed (answered or gone), expired,
    not_yours (asked of someone else), own (the inviter's own invite), already_in (they hold a
    seat in another party of this kind and subject)."""

    def __init__(self, reason: str, seat: "Seat | None" = None):
        super().__init__(reason)
        self.reason, self.seat = reason, seat


def find_code(text: str | None) -> str | None:
    """A seat's code in a message ("... #K7Q2MX ..."), uppercased; None if there is none."""
    m = _CODE.search(text or "")
    return m.group(1).upper() if m else None


def _code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def _now():
    return func.clock_timestamp()


@dataclass
class Parties:
    """Parties of the app's kinds, on its tables."""

    t: RuntimeTables
    pt: PartyTables
    kinds: Mapping[str, Kind] = field(default_factory=dict)
    replies: expect.Replies = field(default_factory=expect.Replies)

    def kind(self, name: str) -> Kind:
        if name not in self.kinds:
            raise KeyError(f"no party kind {name!r}")
        return self.kinds[name]

    # --- reading -------------------------------------------------------------------------------

    def party(self, conn: Connection, party_id: uuid.UUID) -> Party | None:
        p = self.pt.parties
        row = conn.execute(select(p).where(p.c.id == party_id)).first()
        return Party.of(row) if row else None

    def seat(self, conn: Connection, seat_id: uuid.UUID) -> Seat | None:
        s = self.pt.seats
        row = conn.execute(select(s).where(s.c.id == seat_id)).first()
        return Seat.of(row) if row else None

    def seats(self, conn: Connection, party_id: uuid.UUID) -> list[Seat]:
        s = self.pt.seats
        rows = conn.execute(select(s).where(s.c.party_id == party_id).order_by(s.c.created_at))
        return [Seat.of(r) for r in rows]

    def held_by(self, conn: Connection, kind: str, subject: str | None, actor_id: uuid.UUID
                ) -> tuple[Party, Seat] | None:  # fmt: skip
        """The party of this kind and subject in which this actor holds an accepted seat."""
        p, s = self.pt.parties, self.pt.seats
        row = conn.execute(
            select(s, p.c.kind.label("p_kind"))
            .join(p, p.c.id == s.c.party_id)
            .where(p.c.kind == kind, p.c.subject == subject, s.c.actor_id == actor_id,
                   s.c.state == SeatState.ACCEPTED.value,
                   p.c.state != PartyState.CANCELLED.value)
            .limit(1)
        ).first()  # fmt: skip
        if row is None:
            return None
        seat = Seat.of(row)
        return self.party(conn, seat.party_id), seat  # type: ignore[return-value]

    def by_code(self, conn: Connection, code: str) -> Seat | None:
        s = self.pt.seats
        row = conn.execute(select(s).where(s.c.code == code.upper())).first()
        return Seat.of(row) if row else None

    def invited_by(self, conn: Connection, kind: str, subject: str | None, inviter: uuid.UUID
                   ) -> list[Seat]:  # fmt: skip
        """Open invitations this actor made in parties of this kind and subject (door: claim)."""
        p, s = self.pt.parties, self.pt.seats
        rows = conn.execute(
            select(s)
            .join(p, p.c.id == s.c.party_id)
            .where(p.c.kind == kind, p.c.subject == subject, s.c.invited_by == inviter,
                   s.c.state == SeatState.INVITED.value,
                   (s.c.expires_at.is_(None)) | (s.c.expires_at > _now()))
            .order_by(s.c.created_at)
        )  # fmt: skip
        return [Seat.of(r) for r in rows]

    # --- making and inviting -------------------------------------------------------------------

    def create(
        self,
        conn: Connection,
        kind: str,
        *,
        subject: str | None,
        by: uuid.UUID,
        role: str = "host",
        data: dict[str, Any] | None = None,
    ) -> tuple[Party, Seat]:
        """A party of `kind` with its creator in the first seat (accepted)."""
        k = self.kind(kind)
        p, s = self.pt.parties, self.pt.seats
        row = conn.execute(
            insert(p).values(kind=kind, subject=subject, min_seats=k.capacity.min,
                             max_seats=k.capacity.max, created_by=by, data=data or {})
            .returning(p)
        ).one()  # fmt: skip
        party = Party.of(row)
        seat_row = conn.execute(
            insert(s).values(party_id=party.id, role=role, state=SeatState.ACCEPTED.value,
                             actor_id=by, door=Door.CREATED.value, answered_at=_now())
            .returning(s)
        ).one()  # fmt: skip
        seat = Seat.of(seat_row)
        self._emit(conn, "seat.accepted", party, seat, Door.CREATED)
        return self._maybe_confirmed(conn, party), seat

    def invite(self, conn: Connection, party_id: uuid.UUID, *, by: uuid.UUID,
               name: str | None = None, address: Address | None = None, role: str = "guest",
               ttl: timedelta | None = None) -> Seat:  # fmt: skip
        """An invited seat, with its code; `address` is where `ask` may reach them. SeatError
        "full" when the party's ceiling is held already, "closed" for a cancelled party."""
        p, s = self.pt.parties, self.pt.seats
        row = conn.execute(select(p).where(p.c.id == party_id).with_for_update()).first()
        if row is None or row.state == PartyState.CANCELLED.value:
            raise SeatError("closed")
        party = Party.of(row)
        if party.capacity.max is not None and self._held(conn, party.id) >= party.capacity.max:
            raise SeatError("full")
        ttl = ttl or self.kind(party.kind).invite_ttl
        for _ in range(5):  # a clash among 31^6 codes is rare; try again rather than fail
            code = _code()
            if self.by_code(conn, code) is None:
                break
        seat_row = conn.execute(
            insert(s).values(
                party_id=party.id, role=role, state=SeatState.INVITED.value, name=name,
                address_scheme=address.scheme if address else None,
                address_value=address.value if address else None, code=code, invited_by=by,
                expires_at=_now() + ttl,
            ).returning(s)
        ).one()  # fmt: skip
        seat = Seat.of(seat_row)
        self._emit(conn, "seat.invited", party, seat, None)
        return seat

    def _held(self, conn: Connection, party_id: uuid.UUID) -> int:
        s = self.pt.seats
        return conn.scalar(
            select(func.count()).where(
                s.c.party_id == party_id,
                s.c.state.in_([x.value for x in HELD]),
                (s.c.state == SeatState.ACCEPTED.value)
                | s.c.expires_at.is_(None) | (s.c.expires_at > _now()),
            )
        ) or 0  # fmt: skip

    # --- the doors -----------------------------------------------------------------------------

    def ask(
        self,
        conn: Connection,
        runtime: Any,
        seat_id: uuid.UUID,
        *,
        channel: str,
        endpoint: Address,
        message: Callable[[dict[str, str]], Outbound],
        address: Address | None = None,
        name: str | None = None,
    ) -> expect.Expectation:
        """Door "address": ask at the seat's address (or `address`), a contact we start (usually
        a template); `message(buttons)` gets {accept|decline: payload}. One live ask per seat:
        asking again renews it (old buttons stop working)."""
        seat = self._open(conn, seat_id)
        to = address or seat.address
        if to is None:
            raise ValueError("this seat has no address to ask at")
        exp = expect.ask(
            conn, runtime, kind=KIND, channel=channel, endpoint=endpoint, address=to,
            name=name or seat.name, options=OPTIONS, ttl=self._left(conn, seat), message=message,
            data={"seat_id": str(seat.id)}, key=f"seat:{seat.id}", created_by=seat.invited_by,
            on_pending="resend",
        )  # fmt: skip
        self._set(conn, seat.id, expectation_id=exp.id)
        return exp

    def ask_in(self, conn: Connection, runtime: Any, seat_id: uuid.UUID, *, channel: str,
               conversation_id: uuid.UUID, identity_id: uuid.UUID, from_number: str, to: str,
               message: Callable[[dict[str, str]], Outbound]) -> expect.Expectation:  # fmt: skip
        """Door "address", inside a conversation the person already has with us (their reply
        window open): the same buttons, checked against the identity they write from."""
        seat = self._open(conn, seat_id)
        key = f"seat:{seat.id}:{identity_id}"  # one live ask per seat and person asked
        live = expect.pending(conn, self.t, KIND, key)
        if live is not None:
            exp, token = expect.renew(conn, self.t, live.id, self._left(conn, seat))
        else:
            exp, token = expect.expect(
                conn, self.t, kind=KIND, channel=channel, options=OPTIONS,
                ttl=self._left(conn, seat), identity_id=identity_id,
                data={"seat_id": str(seat.id)}, key=key, created_by=seat.invited_by,
            )  # fmt: skip
        built = message({o: expect.payload(token, o) for o in OPTIONS})
        built = replace(built, ref=f"{expect.PREFIX}:{exp.id}")
        turn = runtime.enqueue(
            conn, channel=channel, conversation_id=conversation_id, from_number=from_number,
            to=to, recipient_identity_id=identity_id, messages=[built],
        )  # fmt: skip
        e = self.t.expectations
        conn.execute(update(e).where(e.c.id == exp.id).values(turn_id=turn, updated_at=_now()))
        self._set(conn, seat.id, expectation_id=exp.id)
        return expect.get(conn, self.t, exp.id)  # type: ignore[return-value]

    def redeem(self, conn: Connection, code: str, actor_id: uuid.UUID) -> Outcome:
        """Door "code": take the seat whose code this is."""
        seat = self.by_code(conn, code)
        if seat is None:
            raise SeatError("closed")
        return self.accept(conn, seat.id, actor_id, Door.CODE)

    def handler(self) -> expect.Handler:
        """Answers to `ask` / `ask_in`: accept takes the seat, decline gives it up; an ask that
        ends unanswered (expired, undeliverable) expires the seat. Replies: the kind's handler."""

        def on_answer(conn: Connection, exp: expect.Expectation, routed: Routed):
            seat_id = uuid.UUID(exp.data["seat_id"])
            try:
                if exp.answer == ACCEPT:
                    return list(self.accept(conn, seat_id, routed.actor_id, Door.ADDRESS).replies)
                return list(self.decline(conn, seat_id, routed.actor_id).replies)
            except SeatError as e:
                return [Outbound.text_message(self._why(e.reason))]

        def on_close(conn: Connection, exp: expect.Expectation) -> None:
            self.expire(conn, uuid.UUID(exp.data["seat_id"]))

        return expect.Handler(on_answer, on_close, replies=self.replies)

    def _why(self, reason: str) -> str:
        r = self.replies
        return {"expired": r.expired, "not_yours": r.not_yours, "closed": r.answered}.get(
            reason, r.unknown
        )

    # --- taking and giving up ------------------------------------------------------------------

    def accept(self, conn: Connection, seat_id: uuid.UUID, actor_id: uuid.UUID, door: Door
               ) -> Outcome:  # fmt: skip
        """Take an invited seat, once. SeatError: closed, expired, full, own, already_in.
        Runs the kind's `on_accepted` and closes any live ask for it."""
        seat = self._locked(conn, seat_id)
        party = self.party(conn, seat.party_id)
        assert party is not None
        if seat.state != SeatState.INVITED or party.state == PartyState.CANCELLED:
            raise SeatError("closed", seat)
        if seat.expires_at is not None and seat.expires_at <= conn.scalar(select(_now())):
            self.expire(conn, seat.id)
            raise SeatError("expired", seat)
        if actor_id == seat.invited_by:
            raise SeatError("own", seat)
        if self.held_by(conn, party.kind, party.subject, actor_id) is not None:
            raise SeatError("already_in", seat)
        taken = self._move(conn, seat.id, SeatState.ACCEPTED, actor_id=actor_id,
                           door=door.value, answered_at=_now())  # fmt: skip
        if taken is None:
            raise SeatError("closed", seat)
        self._close_asks(conn, seat.id)  # answered: no other door stays open
        self._propose_address(conn, taken, actor_id, door)
        self._emit(conn, "seat.accepted", party, taken, door)
        party = self._maybe_confirmed(conn, party)
        h = self.kind(party.kind).handler
        replies = tuple(h.on_accepted(conn, party, taken, door)) if h else ()
        return Outcome(party, taken, replies)

    def decline(self, conn: Connection, seat_id: uuid.UUID, actor_id: uuid.UUID | None
                ) -> Outcome:  # fmt: skip
        """Give up an invited seat (they said no), once."""
        return self._release(conn, seat_id, SeatState.INVITED, SeatState.DECLINED, actor_id,
                             "seat.declined")  # fmt: skip

    def expire(self, conn: Connection, seat_id: uuid.UUID) -> Seat | None:
        """An invited seat nobody took in time (an ask's sweep, or found late)."""
        try:
            return self._release(conn, seat_id, SeatState.INVITED, SeatState.EXPIRED, None,
                                 "seat.expired").seat  # fmt: skip
        except SeatError:
            return None

    def leave(self, conn: Connection, seat_id: uuid.UUID, actor_id: uuid.UUID) -> Outcome:
        """Someone in a taken seat leaves it."""
        seat = self.seat(conn, seat_id)
        if seat is None or seat.actor_id != actor_id:
            raise SeatError("not_yours", seat)
        return self._release(conn, seat_id, SeatState.ACCEPTED, SeatState.LEFT, actor_id,
                             "seat.left")  # fmt: skip

    def _release(self, conn, seat_id, before: SeatState, after: SeatState, actor_id, fact
                 ) -> Outcome:  # fmt: skip
        seat = self._locked(conn, seat_id)
        moved = self._move(conn, seat.id, after, expect_state=before, answered_at=_now(),
                           **({"actor_id": actor_id} if actor_id and seat.actor_id is None
                              else {}))  # fmt: skip
        if moved is None:
            raise SeatError("closed", seat)
        party = self.party(conn, seat.party_id)
        assert party is not None
        self._close_asks(conn, seat.id)
        self._emit(conn, fact, party, moved, None)
        h = self.kind(party.kind).handler
        return Outcome(party, moved, tuple(h.on_released(conn, party, moved)) if h else ())

    # --- inside --------------------------------------------------------------------------------

    def _close_asks(self, conn: Connection, seat_id: uuid.UUID) -> None:
        """Cancel the seat's asks still waiting (at its address, in a conversation): it was
        answered, or is gone."""
        e = self.t.expectations
        live = conn.scalars(
            select(e.c.id).where(e.c.kind == KIND, e.c.state == "pending",
                                 e.c.data["seat_id"].astext == str(seat_id))
        ).all()  # fmt: skip
        for exp_id in live:
            expect.cancel(conn, self.t, exp_id)

    def _propose_address(self, conn: Connection, seat: Seat, actor_id: uuid.UUID, door: Door
                         ) -> None:  # fmt: skip
        """The inviter gave an address for this seat and someone took it: if nobody owns that
        address yet, it may be theirs (a `proposed` claim, never a link: the inviter typed it).
        The channel links it later (a reply or receipt naming both), or a person decides."""
        if seat.address is None:
            return
        identity = find(conn, self.t, seat.address)
        if identity is None or owner(conn, self.t, identity) is not None:
            return
        propose(conn, self.t, identity, actor_id, "invitation",
                {"seat_id": str(seat.id), "door": door.value})  # fmt: skip

    def _open(self, conn: Connection, seat_id: uuid.UUID) -> Seat:
        seat = self.seat(conn, seat_id)
        if seat is None or seat.state != SeatState.INVITED:
            raise SeatError("closed", seat)
        return seat

    def _left(self, conn: Connection, seat: Seat) -> timedelta:
        """How long the seat's invitation still runs (an ask lasts as long as the seat)."""
        if seat.expires_at is None:
            return self.kind(self.party(conn, seat.party_id).kind).invite_ttl  # type: ignore[union-attr]
        left = seat.expires_at - conn.scalar(select(_now()))
        return max(left, timedelta(minutes=1))

    def _locked(self, conn: Connection, seat_id: uuid.UUID) -> Seat:
        s = self.pt.seats
        row = conn.execute(select(s).where(s.c.id == seat_id).with_for_update()).first()
        if row is None:
            raise SeatError("closed")
        return Seat.of(row)

    def _set(self, conn: Connection, seat_id: uuid.UUID, **values: Any) -> None:
        s = self.pt.seats
        conn.execute(update(s).where(s.c.id == seat_id).values(updated_at=_now(), **values))

    def _move(self, conn, seat_id, state: SeatState, expect_state: SeatState = SeatState.INVITED,
              **values: Any) -> Seat | None:  # fmt: skip
        s = self.pt.seats
        row = conn.execute(
            update(s)
            .where(s.c.id == seat_id, s.c.state == expect_state.value)
            .values(state=state.value, updated_at=_now(), **values)
            .returning(s)
        ).first()
        return Seat.of(row) if row else None

    def _maybe_confirmed(self, conn: Connection, party: Party) -> Party:
        s, p = self.pt.seats, self.pt.parties
        if party.state != PartyState.FORMING:
            return party
        accepted = conn.scalar(
            select(func.count()).where(s.c.party_id == party.id,
                                       s.c.state == SeatState.ACCEPTED.value)
        )  # fmt: skip
        if (accepted or 0) < party.capacity.min:
            return party
        row = conn.execute(
            update(p)
            .where(p.c.id == party.id, p.c.state == PartyState.FORMING.value)
            .values(state=PartyState.CONFIRMED.value, updated_at=_now())
            .returning(p)
        ).first()
        if row is None:
            return party
        confirmed = Party.of(row)
        data = {"party_id": str(party.id), "kind": party.kind, "subject": party.subject}
        emit(conn, self.t, "party.confirmed", subject_id=party.id, data=data)
        return confirmed

    def _emit(self, conn, kind: str, party: Party, seat: Seat, door: Door | None) -> None:
        emit(
            conn, self.t, kind, subject_id=seat.id,
            data={"party_id": str(party.id), "kind": party.kind, "subject": party.subject,
                  "door": door.value if door else None, "role": seat.role},
        )  # fmt: skip
