"""Dues: one amount an actor owes for one of the app's subjects, until a deadline (meydan.pay).

Experimental (docs/pay.md). A due is the *intent*; checkouts (meydan.pay.checkout) are its
*attempts*, opened for the subject `due:<id>`. What a payment turns into is the app's: each kind of
subject ("registration", "ticket", "appointment") has a handler that says whether it can be paid
now and for how much (`payable`), and what a payment, or its loss, means (`on_paid`,
`on_released`). meydan never owns capacity, prices or rules.

The due's link is a meydan expectation (kind `pay`, keyed by the due): the token goes out in the
person's pay link and is looked up by its hash. The same link is sent more than once (the first
card, a reminder, "send it again"), so the token is also kept sealed (meydan.vault) on the due:
old buttons keep working, and nothing readable is stored.

States: `open` until paid or the deadline; `paid` while a checkout of it is paid; `refund_pending`
/ `refunded` when the provider gives the money back (nothing paid anymore); `expired` (the deadline
passed, nothing paid) and `cancelled` (closed by the app). Each transition happens once
(`UPDATE ... WHERE state = ...`), so a result reported twice can't fulfil twice; the app's handler
runs in the same transaction.
"""

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    Table,
    Text,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import UUID

from meydan.ids import uuid7
from meydan.pay.checkout import Checkout, Status
from meydan.runtime import expect
from meydan.runtime.tables import RuntimeTables
from meydan.vault import Keyring

log = logging.getLogger(__name__)

KIND = "pay"  # the expectations behind pay links
PAID = "paid"  # their one option
SUBJECT = "due"  # checkouts of a due are for "due:<id>"


class DueState(StrEnum):
    OPEN = "open"
    PAID = "paid"
    REFUND_PENDING = "refund_pending"
    REFUNDED = "refunded"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


RELEASED = (DueState.REFUND_PENDING, DueState.REFUNDED, DueState.EXPIRED, DueState.CANCELLED)


def define_table(metadata: MetaData, *, prefix: str = "meydan_", name: str = "pay_dues") -> Table:
    """The dues table, on the app's metadata, named `name`; it refers to meydan's runtime tables
    (actors, expectations), which carry `prefix`."""
    states = ", ".join(repr(str(s)) for s in DueState)
    table = Table(
        name,
        metadata,
        Column("id", UUID(as_uuid=True), primary_key=True, default=uuid7),
        Column("subject", Text, nullable=False),  # the app's: "registration:<uuid>"
        Column("actor_id", UUID(as_uuid=True), ForeignKey(f"{prefix}actors.id"), nullable=False),
        Column("state", Text, nullable=False),
        Column("amount", Numeric(10, 2)),  # as the app said when it opened; the provider's rules
        Column("currency", Text),
        Column("deadline", DateTime(timezone=True), nullable=False),  # the link pays until then
        # after paying the link still opens (the person's own "paid" page) until then; null: always
        Column("readable_until", DateTime(timezone=True)),
        Column("expectation_id", UUID(as_uuid=True), ForeignKey(f"{prefix}expectations.id")),
        Column("link_sealed", Text),  # the link's token, sealed (meydan.vault); never logged
        Column("paid_at", DateTime(timezone=True)),
        Column("closed_at", DateTime(timezone=True)),  # expired / cancelled / refunded
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
        Column(
            "updated_at",
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
        CheckConstraint(f"state IN ({states})", name="state"),
        CheckConstraint("length(subject) BETWEEN 1 AND 200", name="subject"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
    )
    # one live due per subject and payer
    Index(
        f"uq_{name}_open",
        table.c.subject,
        table.c.actor_id,
        unique=True,
        postgresql_where=table.c.state == DueState.OPEN.value,
    )
    Index(f"ix_{name}_deadline", table.c.deadline, postgresql_where=table.c.state == "open")
    return table


@dataclass(frozen=True)
class Due:
    id: uuid.UUID
    subject: str
    actor_id: uuid.UUID
    state: DueState
    amount: Decimal | None
    currency: str | None
    deadline: datetime
    readable_until: datetime | None
    expectation_id: uuid.UUID | None
    paid_at: datetime | None
    closed_at: datetime | None
    created_at: datetime

    @property
    def kind(self) -> str:
        return self.subject.partition(":")[0]

    @property
    def subject_id(self) -> str:
        return self.subject.partition(":")[2]

    @property
    def checkout_subject(self) -> str:
        """What its checkouts are for (meydan.pay.checkout)."""
        return f"{SUBJECT}:{self.id}"

    @classmethod
    def of(cls, row) -> "Due":
        return cls(
            id=row.id, subject=row.subject, actor_id=row.actor_id, state=DueState(row.state),
            amount=row.amount, currency=row.currency, deadline=row.deadline,
            readable_until=row.readable_until, expectation_id=row.expectation_id,
            paid_at=row.paid_at, closed_at=row.closed_at, created_at=row.created_at,
        )  # fmt: skip


@dataclass(frozen=True)
class Payable:
    """The subject can be paid now: how much (as the app knows it) and until when."""

    deadline: datetime
    amount: Decimal | None = None
    currency: str | None = None
    readable_until: datetime | None = None  # after paying, the link opens until then


@dataclass(frozen=True)
class NotPayable:
    """Why not (the app's words for it: "full", "closed", "paid", "gone"...)."""

    reason: str


class Subject(Protocol):
    """The app's meaning of one kind of subject. Each runs in the change's transaction."""

    def payable(
        self, conn: Connection, subject_id: str, actor_id: uuid.UUID
    ) -> Payable | NotPayable:
        """Whether this actor can pay for it now; capacity and eligibility are decided here."""
        ...

    def on_paid(self, conn: Connection, due: Due, checkout: Checkout) -> None:
        """Fulfil: register, issue the ticket, confirm the booking. It may still find it full
        (pay to claim) and decide (an alternate list, a refund)."""
        ...

    def on_released(self, conn: Connection, due: Due, why: DueState) -> None:
        """expired / cancelled / refund_pending / refunded: free what was held, if anything."""
        ...


class DueError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Dues:
    """Opening dues, their links, and what their checkouts mean. Built once (composition root).

    `subjects`: subject kind -> its handler. `keyring` seals the links' tokens. `kind` and
    `channel`: what the links' expectations are recorded as (an app that had pay links of its
    own before keeps their kind, so they still resolve)."""

    def __init__(
        self,
        tables: RuntimeTables,
        table: Table,
        keyring: Keyring,
        subjects: Mapping[str, Subject],
        *,
        kind: str = KIND,
        channel: str = "web",
        now: Callable[[], datetime] = _utcnow,
    ):
        self.tables, self.table, self.keyring = tables, table, keyring
        self.subjects, self.kind, self.channel, self.now = dict(subjects), kind, channel, now

    def subject(self, kind: str) -> Subject:
        try:
            return self.subjects[kind]
        except KeyError:
            raise DueError(f"no handler for subject kind {kind!r}") from None

    # -- reading ------------------------------------------------------------------------------

    def get(self, conn: Connection, due_id: uuid.UUID, *, lock: bool = False) -> Due | None:
        q = select(self.table).where(self.table.c.id == due_id)
        row = conn.execute(q.with_for_update() if lock else q).first()
        return Due.of(row) if row else None

    def live(self, conn: Connection, subject: str, actor_id: uuid.UUID) -> Due | None:
        """The open due for this subject and payer, if any."""
        d = self.table
        row = conn.execute(
            select(d).where(d.c.subject == subject, d.c.actor_id == actor_id, d.c.state == "open")
        ).first()
        return Due.of(row) if row else None

    def _aad(self, due_id: uuid.UUID) -> bytes:
        return f"meydan.pay.due:{due_id}".encode()

    def link(self, conn: Connection, due_id: uuid.UUID) -> str | None:
        """The due's link token, to send again (the same link: old buttons keep working)."""
        sealed = conn.scalar(select(self.table.c.link_sealed).where(self.table.c.id == due_id))
        return self.keyring.open(sealed, aad=self._aad(due_id)).decode() if sealed else None

    def resolve(self, conn: Connection, token: str) -> Due | None:
        """The due a pay link is for, while it opens: open and in time; or paid, until its
        `readable_until` (the person's own "paid" page). None for anything else."""
        e = expect.by_token(conn, self.tables, token, kind=self.kind)
        if e is None or e.key is None:
            return None
        due = self.get(conn, uuid.UUID(e.key))
        if due is None or due.expectation_id != e.id:
            return None  # an older link of a due that has a new one
        now = self.now()
        if due.state == DueState.OPEN:
            return due if now < due.deadline else None
        if due.state == DueState.PAID:
            return due if due.readable_until is None or now < due.readable_until else None
        return None

    # -- opening ------------------------------------------------------------------------------

    def open(
        self, conn: Connection, subject: str, actor_id: uuid.UUID
    ) -> tuple[Due, str] | NotPayable:
        """A due for `subject`, paid by `actor_id`, and its link token; the live one if there is
        one (its deadline and amount brought up to what the app says now). NotPayable when the
        app says it can't be paid now. In the caller's transaction."""
        kind, _, subject_id = subject.partition(":")
        p = self.subject(kind).payable(conn, subject_id, actor_id)
        if isinstance(p, NotPayable):
            return p
        if p.deadline <= self.now():
            return NotPayable("deadline")
        live = self.live(conn, subject, actor_id)
        if live is not None:
            return self._refresh(conn, live, p)
        due_id = uuid7()
        conn.execute(
            insert(self.table).values(
                id=due_id, subject=subject, actor_id=actor_id, state=DueState.OPEN,
                amount=p.amount, currency=p.currency, deadline=p.deadline,
                readable_until=p.readable_until,
            )
        )  # fmt: skip
        token = self._new_link(conn, due_id, p.deadline)
        conn.execute(
            insert(self.tables.audit).values(
                actor_id=actor_id, action=f"due:open:{kind}"[:60], subject_id=due_id
            )
        )
        return self.get(conn, due_id), token  # type: ignore[return-value]

    def _ttl(self, deadline: datetime) -> timedelta:
        # expectations count from the database's clock: the real one
        return max(deadline - _utcnow(), timedelta(seconds=1))

    def _new_link(self, conn: Connection, due_id: uuid.UUID, deadline: datetime) -> str:
        e, token = expect.expect(
            conn, self.tables, kind=self.kind, channel=self.channel, options=[PAID],
            ttl=self._ttl(deadline), key=str(due_id), data={"due": str(due_id)},
            on_pending="allow",
        )  # fmt: skip
        conn.execute(
            update(self.table)
            .where(self.table.c.id == due_id)
            .values(
                expectation_id=e.id,
                link_sealed=self.keyring.seal(token.encode(), aad=self._aad(due_id)),
            )
        )
        return token

    def _refresh(self, conn: Connection, due: Due, p: Payable) -> tuple[Due, str]:
        changes = {
            k: v
            for k, v in (("deadline", p.deadline), ("amount", p.amount), ("currency", p.currency),
                         ("readable_until", p.readable_until))
            if getattr(due, k) != v
        }  # fmt: skip
        if changes:
            conn.execute(update(self.table).where(self.table.c.id == due.id).values(**changes))
        token = self.link(conn, due.id)
        if token is None:
            token = self._new_link(conn, due.id, p.deadline)
        elif "deadline" in changes and due.expectation_id is not None:
            # the same token lives on: only its expectation's expiry follows the new deadline
            ex = self.tables.expectations
            conn.execute(
                update(ex)
                .where(ex.c.id == due.expectation_id, ex.c.state == "pending")
                .values(expires_at=func.now() + self._ttl(p.deadline))
            )
        return self.get(conn, due.id), token  # type: ignore[return-value]

    # -- what happens to it -------------------------------------------------------------------

    def _move(
        self, conn: Connection, due: Due, to: DueState, frm: tuple[DueState, ...], **values
    ) -> Due | None:
        """One transition, once: None if the due wasn't in `frm` (someone else moved it)."""
        row = conn.execute(
            update(self.table)
            .where(self.table.c.id == due.id, self.table.c.state.in_([s.value for s in frm]))
            .values(state=to, **values)
            .returning(self.table)
        ).first()
        if row is None:
            return None
        moved = Due.of(row)
        conn.execute(
            insert(self.tables.audit).values(
                actor_id=moved.actor_id, action=f"due:{to}:{moved.kind}"[:60], subject_id=moved.id
            )
        )
        return moved

    def _paid_elsewhere(self, conn: Connection, checkouts: Table, due: Due, but: uuid.UUID) -> bool:
        return (
            conn.scalar(
                select(func.count())
                .select_from(checkouts)
                .where(
                    checkouts.c.subject == due.checkout_subject,
                    checkouts.c.id != but,
                    checkouts.c.status == Status.PAID.value,
                )
            )
            > 0
        )

    def checkout_handler(
        self, checkouts: Table
    ) -> Callable[[Connection, Checkout, Status | None], None]:
        """The handler for checkouts of subject kind `due` (meydan.pay.checkout.Checkouts
        `subjects={"due": dues.checkout_handler(checkouts_table)}`): a checkout's state -> its
        due's. `paid` pays the due (from open, or expired: paid late is paid); `refund_pending` /
        `refunded` give it back unless another checkout of it is paid; `withdrawn` cancels it.
        `failed` / `expired` attempts change nothing: the due stays open for another try."""

        def handle(conn: Connection, c: Checkout, before: Status | None) -> None:
            due = self.get(conn, uuid.UUID(c.subject_id), lock=True)
            if due is None:
                log.warning("checkout %s: for a due that isn't there", c.id)
                return
            subject = self.subject(due.kind)
            now = self.now()
            if c.status == Status.PAID:
                moved = self._move(conn, due, DueState.PAID,
                                   (DueState.OPEN, DueState.EXPIRED), paid_at=now)  # fmt: skip
                if moved is not None:
                    subject.on_paid(conn, moved, c)
            elif c.status in (Status.REFUND_PENDING, Status.REFUNDED):
                if self._paid_elsewhere(conn, checkouts, due, c.id):
                    return
                to = DueState(c.status.value)
                frm = (DueState.PAID, DueState.REFUND_PENDING)
                moved = self._move(conn, due, to, frm,
                                   closed_at=now if to == DueState.REFUNDED else None)  # fmt: skip
                if moved is not None:
                    subject.on_released(conn, moved, to)
            elif c.status == Status.WITHDRAWN:
                moved = self._move(conn, due, DueState.CANCELLED, (DueState.PAID,), closed_at=now)
                if moved is not None:
                    subject.on_released(conn, moved, DueState.CANCELLED)

        return handle

    def cancel(self, conn: Connection, due_id: uuid.UUID) -> Due | None:
        """Close an open due (the app no longer wants it paid); its link stops working."""
        due = self.get(conn, due_id, lock=True)
        if due is None:
            return None
        moved = self._move(conn, due, DueState.CANCELLED, (DueState.OPEN,), closed_at=self.now())
        if moved is not None:
            if moved.expectation_id is not None:
                expect.cancel(conn, self.tables, moved.expectation_id)
            self.subject(moved.kind).on_released(conn, moved, DueState.CANCELLED)
        return moved

    def expire(self, conn: Connection, limit: int = 100) -> int:
        """Open dues past their deadline become `expired` (the app hears `on_released`)."""
        d = self.table
        ids = (
            conn.execute(
                select(d.c.id)
                .where(d.c.state == "open", d.c.deadline <= self.now())
                .order_by(d.c.deadline)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        done = 0
        for due_id in ids:
            due = self.get(conn, due_id, lock=True)
            if due is None:
                continue
            moved = self._move(conn, due, DueState.EXPIRED, (DueState.OPEN,), closed_at=self.now())
            if moved is not None:
                self.subject(moved.kind).on_released(conn, moved, DueState.EXPIRED)
                done += 1
        return done


__all__ = [
    "KIND", "PAID", "RELEASED", "SUBJECT", "Due", "DueError", "DueState", "Dues", "NotPayable",
    "Payable", "Subject", "define_table",
]  # fmt: skip
