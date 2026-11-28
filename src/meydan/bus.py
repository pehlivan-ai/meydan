"""The bus: domain events and their subscribers. Something happened, everyone who cares hears of it,
each at its own pace, none of them holding up the others (docs/bus.md).

A fact is written once, where it happened, in that transaction (`emit`): if the transaction rolls
back, nothing happened. Each subscription whose kinds (and channel) match gets its own delivery
row, so one slow or failing subscriber never delays another, and no subscriber can miss a fact by
reading a shared log past a transaction that commits late. A subscriber claims its rows in a short
transaction (FOR UPDATE SKIP LOCKED), commits, and only then runs its handler: nothing here is held
open while a handler calls the outside world.

    seen = Subscriber("seen", ("message.ingested",), handle=mark_seen, delay_s=1)
    bus = Bus(engine, tables, [seen])
    await bus.run(stop)                      # or `await events.drain_once()` in tests

    with engine.begin() as conn:                # where it happened
        emit(conn, tables, "message.ingested", subject_id=inbox_id, channel="whatsapp")

Delivery is at least once: a handler may see a fact again (a crash after it acted, a lease that ran
out), so handlers are idempotent. A handler returns None (done), returns `Later(seconds)` (not yet:
try again then, the attempt not counted), raises `PermanentError` (dead now, with the reason) or
raises anything else (tried again with backoff; dead after `max_attempts`). `retry` puts a dead
one back. Subscriptions are rows (`register`), so a fact emitted in one process reaches a
subscriber that runs in another; remove one with `unsubscribe`.

Shapes borrowed, with thanks: the claim-commit-handle cycle and the dead letter in place (Oban,
River), the time as a key column of the claim index and the attempt fence on finish (pg-boss),
emit inside the caller's transaction (River), copies per subscriber (pg-boss), meydan.bridge's
savepoint-free worker shape. See docs/events.md for why not a shared log read by position.
"""

import asyncio
import contextlib
import logging
import random
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, case, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import UUID, insert

from meydan.ids import uuid7
from meydan.runtime.tables import RuntimeTables

log = logging.getLogger(__name__)

ERROR_MAX = 500  # characters of an error kept on a delivery


@dataclass(frozen=True)
class DomainEvent:
    """A fact as a subscriber's handler gets it."""

    id: uuid.UUID
    kind: str
    key: str | None
    subject_id: uuid.UUID | None
    channel: str | None
    data: dict[str, Any]
    at: datetime
    attempt: int  # 1 on the first try of this delivery
    delivery_id: uuid.UUID  # an idempotency key for what the handler does outside


@dataclass(frozen=True)
class Later:
    """Not yet: handle this fact again in `seconds`; the attempt is not counted."""

    seconds: float


class PermanentError(Exception):
    """This fact can never be handled by this subscriber: dead now, with this as the reason."""


Handler = Callable[[DomainEvent], Awaitable["Later | None"]]


@dataclass(frozen=True)
class Subscriber:
    """Who wants which facts, and what it does with each."""

    name: str  # unique in the database; it is the subscription's key
    kinds: tuple[str, ...]
    handle: Handler = field(compare=False)
    channel: str | None = None  # only facts of this channel (and channel-less ones); None: all
    delay_s: float = 0.0  # not before the fact's time plus this ("seen" a second after arrival)
    max_attempts: int = 8
    lease_s: float = 120.0  # a claim held longer than this is taken back (the handler's limit)


def backoff(attempt: int) -> float:
    """Seconds before the next try after `attempt` failures: 15 s + 2^attempt, up to 10% more,
    capped at an hour (Oban's curve: a few quick tries, then hours between the last ones)."""
    base = min(3600.0, 15.0 + 2.0 ** min(attempt, 20))
    return base * (1.0 + random.random() * 0.1)


def emit(
    conn: Connection,
    tables: RuntimeTables,
    kind: str,
    *,
    key: str | None = None,
    subject_id: uuid.UUID | None = None,
    channel: str | None = None,
    data: dict[str, Any] | None = None,
) -> uuid.UUID | None:
    """Record a fact in this transaction and give each matching subscription its copy. With a
    `key`, the same (kind, key) is recorded once: a repeat returns None and delivers nothing."""
    e, s, d = tables.events, tables.event_subscriptions, tables.event_deliveries
    event_id = conn.scalar(
        insert(e)
        .values(
            id=uuid7(),
            kind=kind,
            key=key,
            subject_id=subject_id,
            channel=channel,
            data=data or {},
            at=func.clock_timestamp(),
        )  # fmt: skip
        .on_conflict_do_nothing(index_elements=[e.c.kind, e.c.key])
        .returning(e.c.id)
    )
    if event_id is None:
        return None
    # One statement: a copy for each matching subscription, at its own time.
    match = select(
        func.gen_random_uuid(),
        literal(event_id, UUID(as_uuid=True)),
        s.c.name,
        s.c.max_attempts,
        func.clock_timestamp() + func.make_interval(0, 0, 0, 0, 0, 0, s.c.delay_s),
    ).where(
        s.c.kinds.any_() == kind,
        (s.c.channel.is_(None)) | (literal(channel is None)) | (s.c.channel == channel),
    )
    conn.execute(
        insert(d).from_select(["id", "event_id", "subscriber", "max_attempts", "next_at"], match)
    )
    return event_id


def unsubscribe(conn: Connection, tables: RuntimeTables, name: str) -> bool:
    """Remove a subscription and its deliveries (a subscriber the app no longer runs)."""
    s = tables.event_subscriptions
    return bool(conn.execute(s.delete().where(s.c.name == name)).rowcount)


def retry(conn: Connection, tables: RuntimeTables, delivery_id: uuid.UUID) -> bool:
    """Put a dead delivery back in line, its attempts reset (after fixing what killed it)."""
    d = tables.event_deliveries
    return bool(
        conn.execute(
            update(d)
            .where(d.c.id == delivery_id, d.c.status == "dead")
            .values(
                status="pending",
                attempts=0,
                next_at=func.clock_timestamp(),
                done_at=None,
                lease_until=None,
            )  # fmt: skip
        ).rowcount
    )


class Bus:
    """Runs subscribers: claims their due deliveries, calls their handlers, records what came of
    it; takes back claims whose lease ran out; prunes what is finished."""

    def __init__(
        self,
        engine: Engine,
        tables: RuntimeTables,
        subscribers: Sequence[Subscriber],
        *,
        poll_s: float = 0.5,
        batch: int = 20,
        keep_done: timedelta = timedelta(days=7),
        keep_dead: timedelta = timedelta(days=30),
        keep_events: timedelta = timedelta(days=30),
        prune_every_s: float = 3600.0,
    ):
        names = [sub.name for sub in subscribers]
        if len(set(names)) != len(names):
            raise ValueError(f"subscriber names must be unique: {names}")
        self.engine, self.t = engine, tables
        self.subscribers = {sub.name: sub for sub in subscribers}
        self.poll_s, self.batch = poll_s, batch
        self.keep_done, self.keep_dead, self.keep_events = keep_done, keep_dead, keep_events
        self.prune_every_s = prune_every_s
        self._registered = False

    def register(self) -> None:
        """Write this process's subscriptions (kinds, channel, delay, attempts), so facts emitted
        anywhere reach them. Safe to repeat; run before the first fact they should see."""
        s = self.t.event_subscriptions
        with self.engine.begin() as conn:
            for sub in self.subscribers.values():
                values = {"name": sub.name, "kinds": list(sub.kinds), "channel": sub.channel,
                          "delay_s": sub.delay_s, "max_attempts": sub.max_attempts}  # fmt: skip
                stmt = insert(s).values(values)
                conn.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[s.c.name],
                        set_={k: stmt.excluded[k] for k in values if k != "name"},
                    )
                )
        self._registered = True

    def ensure_registered(self) -> None:
        """`register` once per process (cheap after the first call)."""
        if not self._registered:
            self.register()

    # --- one pass ---------------------------------------------------------------------------

    async def drain_once(self) -> int:
        """Take back expired claims, then handle what is due for each subscriber (up to `batch`
        each). Returns how many deliveries were handled."""
        await asyncio.to_thread(self.ensure_registered)
        await asyncio.to_thread(self.rescue)
        done = 0
        for sub in self.subscribers.values():
            for row in await asyncio.to_thread(self._claim, sub):
                await self._handle(sub, row)
                done += 1
        return done

    def _claim(self, sub: Subscriber) -> list[dict[str, Any]]:
        """Claim due deliveries in a short transaction that commits at once (the handler runs
        after, outside it)."""
        d, e = self.t.event_deliveries.name, self.t.events.name
        sql = text(f"""
            WITH due AS (
                SELECT id FROM {d}
                WHERE subscriber = :sub AND status = 'pending' AND next_at <= clock_timestamp()
                ORDER BY next_at, id LIMIT :n
                FOR UPDATE SKIP LOCKED
            )
            UPDATE {d} AS x
               SET status = 'running', attempts = x.attempts + 1,
                   lease_until = clock_timestamp() + make_interval(secs => :lease)
              FROM due, {e} AS ev
             WHERE x.id = due.id AND ev.id = x.event_id
         RETURNING x.id AS delivery_id, x.attempts, x.max_attempts, ev.id, ev.kind, ev.key,
                   ev.subject_id, ev.channel, ev.data, ev.at
        """)  # noqa: S608 - table names come from our own metadata, values are bound
        with self.engine.begin() as conn:
            rows = conn.execute(sql, {"sub": sub.name, "n": self.batch, "lease": sub.lease_s})
            return [dict(r) for r in rows.mappings()]

    async def _handle(self, sub: Subscriber, row: dict[str, Any]) -> None:
        event = DomainEvent(
            id=row["id"], kind=row["kind"], key=row["key"], subject_id=row["subject_id"],
            channel=row["channel"], data=row["data"] or {}, at=row["at"],
            attempt=row["attempts"], delivery_id=row["delivery_id"],
        )  # fmt: skip
        try:
            async with asyncio.timeout(sub.lease_s):
                outcome = await sub.handle(event)
        except PermanentError as e:
            await asyncio.to_thread(self._finish, row, "dead", str(e))
        except Exception as e:  # noqa: BLE001 - recorded and retried, never lost
            error = f"{type(e).__name__}: {e}"
            if row["attempts"] >= row["max_attempts"]:
                log.warning("event %s: %s gave up after %d tries", event.id, sub.name,
                            row["attempts"])  # fmt: skip
                await asyncio.to_thread(self._finish, row, "dead", error)
            else:
                await asyncio.to_thread(self._again, row, backoff(row["attempts"]), error, True)
        else:
            if isinstance(outcome, Later):
                await asyncio.to_thread(self._again, row, outcome.seconds, None, False)
            else:
                await asyncio.to_thread(self._finish, row, "done", None)

    def _fence(self, row: dict[str, Any]):
        """Only the claim that is still ours: a claim taken back and given out again isn't."""
        d = self.t.event_deliveries
        return (
            (d.c.id == row["delivery_id"])
            & (d.c.status == "running")
            & (d.c.attempts == row["attempts"])
        )

    def _finish(self, row: dict[str, Any], status: str, error: str | None) -> None:
        d = self.t.event_deliveries
        with self.engine.begin() as conn:
            conn.execute(
                update(d)
                .where(self._fence(row))
                .values(
                    status=status,
                    done_at=func.clock_timestamp(),
                    lease_until=None,
                    last_error=error[:ERROR_MAX] if error else None,
                )  # fmt: skip
            )

    def _again(self, row: dict[str, Any], seconds: float, error: str | None, counted: bool) -> None:
        d = self.t.event_deliveries
        values: dict[str, Any] = {
            "status": "pending",
            "lease_until": None,
            "next_at": func.clock_timestamp() + timedelta(seconds=max(0.0, seconds)),
        }
        if error is not None:
            values["last_error"] = error[:ERROR_MAX]
        if not counted:
            values["attempts"] = d.c.attempts - 1
        with self.engine.begin() as conn:
            conn.execute(update(d).where(self._fence(row)).values(values))

    def rescue(self) -> int:
        """Claims whose lease ran out (the process died, a handler hung): tried again, or dead
        when that was the last attempt."""
        d = self.t.event_deliveries
        last = d.c.attempts >= d.c.max_attempts
        now = func.clock_timestamp()
        with self.engine.begin() as conn:
            return conn.execute(
                update(d)
                .where(d.c.status == "running", d.c.lease_until < now)
                .values(
                    status=case((last, "dead"), else_="pending"),
                    done_at=case((last, now), else_=None),
                    next_at=now,
                    lease_until=None,
                    last_error="lease ran out",
                )
            ).rowcount

    def prune(self, limit: int = 10_000) -> int:
        """Delete finished deliveries past their retention (done: `keep_done`; dead: `keep_dead`),
        then facts older than `keep_events` that no unfinished delivery needs. Batched."""
        d, e = self.t.event_deliveries, self.t.events
        now = func.clock_timestamp()
        old = ((d.c.status == "done") & (d.c.done_at < now - self.keep_done)) | (
            (d.c.status == "dead") & (d.c.done_at < now - self.keep_dead)
        )
        with self.engine.begin() as conn:
            gone = conn.execute(
                d.delete().where(d.c.id.in_(select(d.c.id).where(old).limit(limit)))
            ).rowcount
            open_ = select(d.c.id).where(
                d.c.event_id == e.c.id, d.c.status.in_(("pending", "running"))
            )
            gone += conn.execute(
                e.delete().where(
                    e.c.id.in_(
                        select(e.c.id)
                        .where(e.c.at < now - self.keep_events, ~open_.exists())
                        .limit(limit)
                    )
                )
            ).rowcount
        return gone

    def backlog(self) -> dict[str, Any]:
        """For health and ops: deliveries waiting (and the oldest one's age in seconds), dead."""
        d = self.t.event_deliveries
        now = func.clock_timestamp()
        with self.engine.connect() as conn:
            row = conn.execute(
                select(
                    func.count().filter(d.c.status == "pending", d.c.next_at <= now),
                    func.coalesce(
                        func.max(func.extract("epoch", now - d.c.next_at)).filter(
                            d.c.status == "pending", d.c.next_at <= now
                        ),
                        0,
                    ),
                    func.count().filter(d.c.status == "dead"),
                )
            ).one()
        return {"due": int(row[0]), "oldest_s": float(row[1]), "dead": int(row[2])}

    # --- the loop -------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Handle deliveries until `stop` is set: right away while there is work, every `poll_s`
        otherwise; prune every `prune_every_s`. Run as a background task."""
        await asyncio.to_thread(self.register)
        pruned_at = 0.0
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            handled = 0
            try:
                handled = await self.drain_once()
                if loop.time() - pruned_at >= self.prune_every_s:
                    await asyncio.to_thread(self.prune)
                    pruned_at = loop.time()
            except Exception:
                log.exception("events loop error")
            if handled:
                continue  # more may be waiting
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.poll_s)
