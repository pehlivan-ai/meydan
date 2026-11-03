"""Waiting for an answer: a stored expectation that an incoming message completes.

The durable-promise model (docs/expect.md): a record made before anything is sent, with a token
the answer must carry and an expiry. It ends once, in one of four ways, and the app is told:

    pending -> answered (with one of its options) | expired | cancelled | undeliverable

- `expect(...)` records it; `ask(...)` also sends a template whose buttons carry the token, to an
  address (`plan_contact`: nobody is created on a guess), with `ref="expect:<id>"` so delivery,
  reads and answers are followed like any message (observe.interactions, engagement).
- `Expectations(tables, {kind: Handler})` gives the runtime its interceptor (an answer is matched
  by token, checked, completed once, then the app's `on_answer` runs) and its sweep (due ones
  expire, ones whose message couldn't be delivered become undeliverable; the app's `on_close`).
- Meaning stays in the app: what an accepted invite does is its `on_answer`.

Checks on every answer: the token is known; the answer points at the message that asked (when
the channel says which); the sender owns the address it was asked of (if one was named); it is
still pending and not past its time; the option is one of its options.
"""

import hashlib
import secrets
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import Connection, Engine, func, insert, select, update

from meydan.channels.base import Address, Inbound, Outbound
from meydan.runtime.identity import Routed, owner, plan_contact
from meydan.runtime.inbound import replied_to
from meydan.runtime.tables import RuntimeTables
from meydan.runtime.types import Intercepted

if TYPE_CHECKING:
    from meydan.runtime.core import Runtime

PREFIX = "expect"
OnPending = Literal["refuse", "resend", "allow"]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now():
    return func.clock_timestamp()


def payload(token: str, option: str) -> str:
    """What a button sends back: expect:<token>:<option>."""
    return f"{PREFIX}:{token}:{option}"


@dataclass(frozen=True)
class Expectation:
    id: uuid.UUID
    kind: str
    channel: str
    identity_id: uuid.UUID | None
    key: str | None
    options: tuple[str, ...]
    data: dict[str, Any]
    state: str
    answer: str | None
    answered_by: uuid.UUID | None
    answered_at: datetime | None
    expires_at: datetime
    turn_id: uuid.UUID | None
    created_by: uuid.UUID | None

    @classmethod
    def of(cls, row: Any) -> "Expectation":
        return cls(
            id=row.id, kind=row.kind, channel=row.channel, identity_id=row.identity_id,
            key=row.key, options=tuple(row.options or ()), data=dict(row.data or {}),
            state=row.state, answer=row.answer, answered_by=row.answered_by,
            answered_at=row.answered_at, expires_at=row.expires_at, turn_id=row.turn_id,
            created_by=row.created_by,
        )  # fmt: skip


class PendingError(Exception):
    """Something with this kind and key is already waiting for an answer (on_pending="refuse")."""

    def __init__(self, expectation_id: uuid.UUID):
        super().__init__(str(expectation_id))
        self.expectation_id = expectation_id


def get(conn: Connection, t: RuntimeTables, expectation_id: uuid.UUID) -> Expectation | None:
    row = conn.execute(select(t.expectations).where(t.expectations.c.id == expectation_id)).first()
    return Expectation.of(row) if row else None


def pending(conn: Connection, t: RuntimeTables, kind: str, key: str) -> Expectation | None:
    """The live, unanswered expectation for this kind and key, if any."""
    e = t.expectations
    row = conn.execute(
        select(e)
        .where(e.c.kind == kind, e.c.key == key, e.c.state == "pending", e.c.expires_at > _now())
        .order_by(e.c.created_at.desc())
        .limit(1)
    ).first()
    return Expectation.of(row) if row else None


def expect(
    conn: Connection,
    t: RuntimeTables,
    *,
    kind: str,
    channel: str,
    options: Sequence[str],
    ttl: timedelta,
    identity_id: uuid.UUID | None = None,
    data: dict[str, Any] | None = None,
    key: str | None = None,
    created_by: uuid.UUID | None = None,
    on_pending: OnPending = "refuse",
) -> tuple[Expectation, str]:
    """Record what we wait for. Returns it and its token (only the token's hash is kept).

    With a `key`, a live one with the same kind and key decides by `on_pending`: "refuse"
    (PendingError), "resend" (the same expectation with a new token and expiry: old buttons stop
    working) or "allow" (a second one)."""
    if key is not None and on_pending != "allow":
        live = pending(conn, t, kind, key)
        if live is not None:
            if on_pending == "refuse":
                raise PendingError(live.id)
            return renew(conn, t, live.id, ttl)
    token = secrets.token_urlsafe(16)
    row = conn.execute(
        insert(t.expectations)
        .values(
            kind=kind, channel=channel, identity_id=identity_id, key=key,
            token_hash=_hash(token), options=list(options), data=data or {},
            expires_at=_now() + ttl, created_by=created_by,
        )
        .returning(t.expectations)
    ).one()  # fmt: skip
    return Expectation.of(row), token


def renew(
    conn: Connection, t: RuntimeTables, expectation_id: uuid.UUID, ttl: timedelta
) -> tuple[Expectation, str]:
    """A new token and expiry for a pending expectation (the old token stops working)."""
    e = t.expectations
    token = secrets.token_urlsafe(16)
    row = conn.execute(
        update(e)
        .where(e.c.id == expectation_id, e.c.state == "pending")
        .values(token_hash=_hash(token), expires_at=_now() + ttl, updated_at=_now())
        .returning(e)
    ).first()
    if row is None:
        raise ValueError(f"expectation {expectation_id} is not pending")
    return Expectation.of(row), token


def cancel(conn: Connection, t: RuntimeTables, expectation_id: uuid.UUID) -> bool:
    """Close a pending expectation as cancelled; an answer after that gets `Replies.cancelled`."""
    return _close(conn, t, expectation_id, "cancelled") is not None


def _close(
    conn: Connection, t: RuntimeTables, expectation_id: uuid.UUID, state: str, **values: Any
) -> Expectation | None:
    e = t.expectations
    row = conn.execute(
        update(e)
        .where(e.c.id == expectation_id, e.c.state == "pending")
        .values(state=state, closed_at=_now(), updated_at=_now(), **values)
        .returning(e)
    ).first()
    return Expectation.of(row) if row else None


def ask(
    conn: Connection,
    runtime: "Runtime",
    *,
    kind: str,
    channel: str,
    endpoint: Address,
    address: Address,
    message: Callable[[dict[str, str]], Outbound],
    options: Sequence[str],
    ttl: timedelta,
    name: str | None = None,
    data: dict[str, Any] | None = None,
    key: str | None = None,
    created_by: uuid.UUID | None = None,
    on_pending: OnPending = "refuse",
) -> Expectation:
    """Ask someone at `address` and wait for their answer.

    `message(buttons)` builds the Outbound (usually a template) from {option: payload}; put the
    payloads in your template's button order. It goes from our `endpoint` to `address`, which is
    a contact we start: settled by nobody's guess (identity.plan_contact)."""
    t = runtime.t
    planned = plan_contact(
        conn, t, channel=channel, to=endpoint, address=address, name=name
    )  # fmt: skip
    exp, token = expect(
        conn, t, kind=kind, channel=channel, options=options, ttl=ttl,
        identity_id=planned.identity_id, data=data, key=key, created_by=created_by,
        on_pending=on_pending,
    )  # fmt: skip
    built = message({option: payload(token, option) for option in options})
    built = replace(built, ref=f"{PREFIX}:{exp.id}")
    turn = runtime.enqueue(
        conn, channel=channel, conversation_id=planned.conversation_id,
        from_number=endpoint.value, to=address.value, recipient_identity_id=planned.identity_id,
        messages=[built],
    )  # fmt: skip
    e = t.expectations
    conn.execute(update(e).where(e.c.id == exp.id).values(turn_id=turn, updated_at=_now()))
    return get(conn, t, exp.id)  # type: ignore[return-value]


@dataclass(frozen=True)
class Replies:
    """What the person is told when an answer can't complete it. The app gives its language."""

    unknown: str = "This is no longer valid."
    not_yours: str = "This wasn't sent to this number."
    answered: str = "This was already answered."
    cancelled: str = "This was cancelled."
    expired: str = "This has expired."


@dataclass(frozen=True)
class Handler:
    """What one kind of expectation means to the app."""

    # A valid answer: do what it means (in the same transaction), return the replies to send.
    on_answer: Callable[[Connection, Expectation, Routed], Sequence[Outbound]]
    # It ended without an answer (expired, undeliverable): e.g. remind, or tell whoever asked.
    on_close: Callable[[Connection, Expectation], None] | None = None
    replies: Replies = field(default_factory=Replies)


class Expectations:
    """The runtime's side: match answers (interceptor) and close what's overdue (sweep)."""

    def __init__(
        self,
        tables: RuntimeTables,
        handlers: dict[str, Handler],
        replies: Replies | None = None,
        payload_of: Callable[[Inbound], str | None] | None = None,
    ):
        self.t = tables
        self.handlers = handlers
        self.replies = replies or Replies()  # when the kind is unknown
        if payload_of is None:  # the channel says where a pressed button's payload is
            from meydan.channels.whatsapp import reply_payload as payload_of
        self.payload_of = payload_of

    def intercept(self, conn: Connection, message: Inbound, routed: Routed) -> Intercepted | None:
        raw = self.payload_of(message) or ""
        if not raw.startswith(f"{PREFIX}:"):
            return None
        _, token, option = (raw.split(":", 2) + ["", ""])[:3]
        e = self.t.expectations
        row = conn.execute(
            select(e, (e.c.expires_at > _now()).label("live"))
            .where(e.c.token_hash == _hash(token))
            .with_for_update()
        ).first()
        handler = self.handlers.get(row.kind) if row else None
        if row is None or handler is None:
            return self._say(self.replies.unknown)
        r = handler.replies
        exp = Expectation.of(row)
        if exp.state == "cancelled":
            return self._say(r.cancelled)
        if exp.state == "answered":
            return self._say(r.answered)
        if exp.state != "pending":
            return self._say(r.expired if exp.state == "expired" else r.unknown)
        if not row.live:
            closed = _close(conn, self.t, exp.id, "expired")
            if closed is not None and handler.on_close:
                handler.on_close(conn, closed)
            return self._say(r.expired)
        answered = replied_to(conn, self.t, message)
        if answered is not None and answered.ref not in (None, f"{PREFIX}:{exp.id}"):
            return self._say(r.unknown)  # a token carried on some other message
        if exp.identity_id is not None and owner(conn, self.t, exp.identity_id) != routed.actor_id:
            return self._say(r.not_yours)
        if option not in exp.options:
            return self._say(r.unknown)
        done = _close(
            conn, self.t, exp.id, "answered",
            answer=option, answered_by=routed.actor_id, answered_at=_now(),
        )  # fmt: skip
        return Intercepted(tuple(handler.on_answer(conn, done, routed)))  # type: ignore[arg-type]

    @staticmethod
    def _say(text: str) -> Intercepted:
        return Intercepted((Outbound.text_message(text),))

    def sweep(self, conn: Connection) -> int:
        """Close what's overdue: past its time -> expired; its message dead -> undeliverable.
        Calls each kind's `on_close`. Returns how many were closed."""
        e, o = self.t.expectations, self.t.outbox
        dead = select(o.c.turn_id).where(o.c.status == "dead", o.c.turn_id.is_not(None))
        closed = 0
        for state, cond in (
            ("undeliverable", e.c.turn_id.in_(dead)),
            ("expired", e.c.expires_at <= _now()),
        ):
            rows = conn.execute(
                update(e)
                .where(e.c.state == "pending", cond)
                .values(state=state, closed_at=_now(), updated_at=_now())
                .returning(e)
            ).all()
            for row in rows:
                exp = Expectation.of(row)
                handler = self.handlers.get(exp.kind)
                if handler and handler.on_close:
                    handler.on_close(conn, exp)
            closed += len(rows)
        return closed

    def sweep_once(self, engine: Engine) -> int:
        with engine.begin() as conn:
            return self.sweep(conn)
