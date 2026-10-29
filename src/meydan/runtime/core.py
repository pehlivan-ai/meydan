"""The conversation runtime: inbox → worker (one turn per conversation at a time) → outbox → sender.

Patterns (docs/architecture.md): Idempotent Receiver, Single Writer per conversation (Postgres
advisory lock), Aggregator (debounce bursts into one turn), Resequencer (order by the sender's
timestamp), Transactional Outbox, Rate-limited sender with jittered retries and a dead letter.
Each message is routed to a conversation through its sender's identity (meydan.runtime.identity).
"Seen" and "typing…" follow the configured presence (meydan.runtime.presence); the inbox records
when each happened (`read_at`, `typing_at`), so every turn has a measurable timeline.

All times are taken on the database clock, the same clock the queues are read with, so an app
server whose clock drifts can't stall or rush the sender. They use `clock_timestamp()`, not
`now()`: Postgres' `now()` is the start of the transaction, and a turn's transaction spans the
model's thinking time (pacing measured from it would already be in the past).

Delivery is at least once end to end: if the process dies after the channel accepted a message
but before we recorded it, that message is sent again. Channels offer no idempotency key.
"""

import asyncio
import contextlib
import logging
import random
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import Connection, Engine, case, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert

from meydan.channels.base import Address, Inbound, Outbound, SendError, StatusUpdate
from meydan.observe.context import TurnContext, turn_scope
from meydan.observe.record import write_turn
from meydan.runtime import identity
from meydan.runtime.presence import Presence
from meydan.runtime.tables import RuntimeTables

log = logging.getLogger("meydan.runtime")


def _clock():
    """The database's wall clock at this statement (see the module docstring: not `now()`)."""
    return func.clock_timestamp()


class Channel(Protocol):
    async def send(self, from_number: str, to: str, message: Outbound) -> str: ...

    # Mark the message (and everything before it) read and show "typing…".
    async def typing(self, from_number: str, message_id: str) -> None: ...

    # Optional: mark read without typing. Channels without it get no separate read receipts.
    # async def mark_read(self, from_number: str, message_id: str) -> None: ...


@dataclass(frozen=True)
class Turn:
    """Everything someone sent since the last turn, in the order they sent it."""

    conversation_id: uuid.UUID
    channel: str
    to: str  # our endpoint (WhatsApp: phone_number_id)
    sender: Address  # where the reply goes (the address of the latest message)
    sender_identity_id: uuid.UUID
    actor_id: uuid.UUID  # who it is, as settled now (memory and domain data hang off this)
    sender_name: str | None
    messages: Sequence[Inbound]

    @property
    def text(self) -> str:
        return "\n".join(m.text for m in self.messages if m.text)


@dataclass(frozen=True)
class Said:
    """One line of a conversation's transcript: something received or sent."""

    at: datetime  # database clock: when we received it / queued it (one clock for both sides)
    direction: str  # "in" | "out"
    identity_id: uuid.UUID | None  # who said it (in) / to whom (out)
    text: str
    turn_id: uuid.UUID | None = None  # outbound: messages of one reply share it


@dataclass(frozen=True)
class Intercepted:
    """A message handled before the agent (e.g. a sign-in code): no turn runs for it, and these
    replies, if any, are sent."""

    replies: Sequence[Outbound] = ()


# Looks at each new message before any turn; returns Intercepted to consume it, None to pass.
Interceptor = Callable[[Connection, Inbound, "identity.Routed"], Intercepted | None]
Handler = Callable[[Turn], Awaitable[Sequence[Outbound]]]
FailureReply = Callable[[Turn, BaseException], Sequence[Outbound]]


@dataclass
class RuntimeConfig:
    debounce_s: float = 2.5  # wait this long after a person's last message before replying
    max_wait_s: float = 8.0  # ...but never longer than this after their first unanswered one
    max_turn_attempts: int = 3  # then give up on those messages (poison message guard)
    max_send_attempts: int = 6  # then dead letter
    chars_per_second: float = 40  # pacing between bubbles ("typing" speed)
    max_bubble_gap_s: float = 3.0
    poll_s: float = 0.5
    batch: int = 10
    # "Seen" and "typing…": a style name ("human", "instant", "quiet") or a Presence with numbers.
    presence: Presence | str = field(default_factory=Presence.human)


_STATUS_RANK = {"pending": 0, "sent": 1, "delivered": 2, "read": 3}


class Runtime:
    def __init__(
        self,
        engine: Engine,
        tables: RuntimeTables,
        channel: Channel,
        handler: Handler,
        config: RuntimeConfig | None = None,
        on_failure: FailureReply | None = None,
        intercept: Sequence[Interceptor] = (),
    ):
        self.engine = engine
        self.t = tables
        self.channel = channel
        self.handler = handler
        self.cfg = config or RuntimeConfig()
        self.presence = Presence.of(self.cfg.presence)
        self.intercept = list(intercept)
        self.on_failure = on_failure

    # --- receiving (call from the webhook, inside the request's transaction) -------------

    def receive(
        self, conn: Connection, messages: Sequence[Inbound], statuses: Sequence[StatusUpdate]
    ) -> int:
        """Store new messages (duplicates are ignored) and apply delivery receipts.

        Returns how many messages were new. Cheap: safe to call before answering 200.
        """
        new = 0
        i = self.t.inbox
        for m in messages:
            seen = select(i.c.id).where(i.c.channel == m.channel, i.c.message_id == m.message_id)
            if conn.scalar(seen) is not None:
                continue  # duplicate delivery: identities and routing were settled the first time
            routed = identity.route(
                conn,
                self.t,
                channel=m.channel,
                to=m.to,
                sender=m.sender,
                also=m.also,
                name=m.sender_name,
                attrs=m.attrs,
                evidence={"message_id": m.message_id},
            )
            row = {
                "channel": m.channel,
                "message_id": m.message_id,
                "conversation_id": routed.conversation_id,
                "sender_identity_id": routed.sender_identity_id,
                "to": m.to.value,
                "sender": m.sender.value,
                "sender_name": m.sender_name,
                "kind": m.kind,
                "text": m.text,
                "payload": m.payload,
                "sent_at": m.sent_at,
            }
            stmt = (
                pg_insert(self.t.inbox)
                .values(row)
                .on_conflict_do_nothing()
                .returning(self.t.inbox.c.id)
            )
            inserted = conn.execute(stmt).all()  # rowcount is unreliable with ON CONFLICT
            new += len(inserted)
            if inserted:
                self._intercept(conn, m, routed, inserted[0][0])
        for s in statuses:
            self._apply_status(conn, s)
        return new

    def _intercept(
        self, conn: Connection, m: Inbound, routed: "identity.Routed", inbox_id: uuid.UUID
    ) -> None:
        """Let interceptors consume a new message before any turn (first one wins)."""
        for intercept in self.intercept:
            result = intercept(conn, m, routed)
            if result is None:
                continue
            i = self.t.inbox
            conn.execute(
                update(i).where(i.c.id == inbox_id).values(processed_at=_clock(), read_at=_clock())
            )
            self.enqueue(
                conn,
                channel=m.channel,
                conversation_id=routed.conversation_id,
                from_number=m.to.value,
                to=m.sender.value,
                recipient_identity_id=routed.sender_identity_id,
                messages=result.replies,
            )
            return

    def _apply_status(self, conn: Connection, s: StatusUpdate) -> None:
        o = self.t.outbox
        cond = o.c.channel_message_id == s.message_id
        if s.status == "failed":
            cond &= o.c.status != "read"
        elif s.status in _STATUS_RANK:
            # Receipts arrive unordered: only move forward (sent -> delivered -> read).
            cond &= case(_STATUS_RANK, value=o.c.status, else_=99) < _STATUS_RANK[s.status]
        else:
            return
        conn.execute(
            update(o).where(cond).values(status=s.status, last_error=s.error, updated_at=_clock())
        )

    # --- enqueueing (replies, and proactive messages from planners) -----------------------

    def enqueue(
        self,
        conn: Connection,
        *,
        channel: str,
        conversation_id: uuid.UUID,
        from_number: str,
        to: str,
        messages: Sequence[Outbound],
        recipient_identity_id: uuid.UUID | None = None,
        start_at: datetime | None = None,
        turn_id: uuid.UUID | None = None,
    ) -> uuid.UUID | None:
        """Queue one outbound turn (one or more messages), paced like someone typing them.

        Returns the turn id. Without `start_at` the turn starts now on the database clock."""
        if not messages:
            return None
        turn_id = turn_id or uuid.uuid4()
        offset = timedelta()
        rows = []
        for seq, m in enumerate(messages):
            if seq:
                gap = len(m.text or "") / self.cfg.chars_per_second
                offset += timedelta(seconds=min(gap, self.cfg.max_bubble_gap_s))
            rows.append(
                {
                    "channel": channel,
                    "conversation_id": conversation_id,
                    "recipient_identity_id": recipient_identity_id,
                    "from_number": from_number,
                    "to": to,
                    "turn_id": turn_id,
                    "seq": seq,
                    "message": m.to_payload(),
                    "created_at": _clock(),
                    "next_attempt_at": (start_at + offset) if start_at else _clock() + offset,
                }
            )
        for row in rows:  # one statement per row: `next_attempt_at` may be a SQL expression
            conn.execute(insert(self.t.outbox).values(row))
        return turn_id

    # --- reading -------------------------------------------------------------------------

    def transcript(
        self, conn: Connection, conversation_id: uuid.UUID, limit: int = 30
    ) -> list[Said]:
        """See `transcript`."""
        return transcript(conn, self.t, conversation_id, limit)

    # --- reader: "seen" shortly after a message arrives ------------------------------------

    def _due_reads(self) -> list[dict[str, Any]]:
        after = self.presence.read_after_s
        if after is None:
            return []
        i = self.t.inbox
        # The latest unread message of each conversation; marking it read covers earlier ones.
        q = (
            select(i.c.conversation_id, i.c.to, i.c.message_id, i.c.received_at)
            .where(
                i.c.read_at.is_(None),
                i.c.processed_at.is_(None),  # answered messages need no separate "seen"
                i.c.received_at <= _clock() - timedelta(seconds=after),
            )
            .order_by(i.c.conversation_id, i.c.sent_at.desc(), i.c.id.desc())
            .ext(distinct_on(i.c.conversation_id))
            .limit(self.cfg.batch)
        )
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    def _set_read(self, conversation_id: uuid.UUID, up_to: datetime) -> None:
        i = self.t.inbox
        with self.engine.begin() as conn:
            conn.execute(
                update(i)
                .where(
                    i.c.conversation_id == conversation_id,
                    i.c.read_at.is_(None),
                    i.c.received_at <= up_to,
                )
                .values(read_at=_clock())
            )

    async def read_once(self) -> int:
        """Send due read receipts (presence.read_after_s). Returns how many conversations."""
        due = await asyncio.to_thread(self._due_reads)
        mark_read = getattr(self.channel, "mark_read", None)
        for row in due:
            if mark_read is not None:
                try:  # cosmetic: never let it break anything
                    await mark_read(row["to"], row["message_id"])
                except Exception:
                    log.warning("read receipt failed for %s", row["conversation_id"], exc_info=True)
            await asyncio.to_thread(self._set_read, row["conversation_id"], row["received_at"])
        return len(due)

    # --- worker: one turn per conversation at a time ---------------------------------------

    def _due_conversations(self) -> list[uuid.UUID]:
        i = self.t.inbox
        now = _clock()
        q = (
            select(i.c.conversation_id)
            .where(i.c.processed_at.is_(None))
            .group_by(i.c.conversation_id)
            .having(
                (func.max(i.c.received_at) <= now - timedelta(seconds=self.cfg.debounce_s))
                | (func.min(i.c.received_at) <= now - timedelta(seconds=self.cfg.max_wait_s))
            )
            .order_by(func.min(i.c.received_at))
            .limit(self.cfg.batch)
        )
        with self.engine.connect() as conn:
            return list(conn.scalars(q))

    async def work_once(self) -> int:
        """Run at most one turn for each conversation that's due. Returns turns run."""
        keys = await asyncio.to_thread(self._due_conversations)
        done = 0
        for key in keys:
            done += await self._turn(key)
        return done

    def _claim(self, conn: Connection, key: uuid.UUID) -> list[dict[str, Any]] | None:
        lock = func.pg_try_advisory_xact_lock(func.hashtextextended(f"conversation:{key}", 0))
        if not conn.scalar(select(lock)):
            return None  # another worker is in a turn for this conversation
        i, ident, cv, cl = self.t.inbox, self.t.identities, self.t.conversations, self.t.claims
        ep = ident.alias("endpoint")
        rows = conn.execute(
            select(
                i,
                ident.c.scheme.label("sender_scheme"),
                ident.c.scope.label("sender_scope"),
                ep.c.scheme.label("to_scheme"),
                ep.c.scope.label("to_scope"),
                cl.c.actor_id,
                # Seconds since "seen", on the database clock (None: not read yet).
                func.extract("epoch", _clock() - i.c.read_at).label("since_read"),
            )
            .join(ident, ident.c.id == i.c.sender_identity_id)
            .join(cv, cv.c.id == i.c.conversation_id)
            .join(ep, ep.c.id == cv.c.endpoint_identity_id)
            .outerjoin(
                cl,
                (cl.c.identity_id == i.c.sender_identity_id)
                & (cl.c.state == "active")
                & cl.c.valid_to.is_(None),
            )
            .where(i.c.conversation_id == key, i.c.processed_at.is_(None))
            .order_by(i.c.sent_at, i.c.id)  # Resequencer: the sender's order, not arrival
        ).mappings()
        return [dict(r) for r in rows]

    async def _turn(self, key: uuid.UUID) -> int:
        conn = await asyncio.to_thread(self.engine.connect)
        try:
            trans = conn.begin()
            rows = await asyncio.to_thread(self._claim, conn, key)
            if not rows:
                trans.rollback()
                return 0
            turn = _turn_from_rows(key, rows)
            ids = [r["id"] for r in rows]
            last = rows[-1]["message_id"]
            started_at = await asyncio.to_thread(conn.scalar, select(_clock()))
            ctx = TurnContext(uuid.uuid4(), key, turn.actor_id)
            refresh = None
            if self.presence.typing:
                await self._read_then_wait(conn, turn, rows)
                await self._typing(turn.to, last, key)
                await asyncio.to_thread(self._stamp, conn, ids, "typing_at")
                if self.presence.typing_refresh_s:
                    refresh = asyncio.create_task(self._keep_typing(turn.to, last, key))
            try:
                with turn_scope(ctx):
                    replies = await self.handler(turn)
            except Exception as e:
                trans.rollback()
                log.exception("turn failed for %s", key)
                await asyncio.to_thread(self._record_failure, turn, ids, e, ctx, started_at)
                return 0
            finally:
                if refresh is not None:
                    refresh.cancel()

            def commit() -> None:
                self.enqueue(
                    conn,
                    channel=turn.channel,
                    conversation_id=key,
                    from_number=turn.to,
                    to=turn.sender.value,
                    recipient_identity_id=turn.sender_identity_id,
                    messages=replies,
                    turn_id=ctx.turn_id,
                )
                self._mark_processed(conn, ids)
                write_turn(
                    conn,
                    self.t,
                    ctx,
                    channel=turn.channel,
                    started_at=started_at,
                    status="replied",
                    messages_in=len(ids),
                    messages_out=len(replies),
                )
                trans.commit()

            await asyncio.to_thread(commit)
            return 1
        finally:
            await asyncio.to_thread(conn.close)

    async def _read_then_wait(
        self, conn: Connection, turn: Turn, rows: list[dict[str, Any]]
    ) -> None:
        """Make sure the person has seen "seen" before "typing…", with the configured gap."""
        since = rows[-1]["since_read"]
        if since is None:  # the reader hasn't marked it yet (or read receipts are off)
            mark_read = getattr(self.channel, "mark_read", None)
            if mark_read is not None and self.presence.read_after_s is not None:
                try:
                    await mark_read(turn.to, rows[-1]["message_id"])
                except Exception:
                    log.warning("read receipt failed for %s", turn.conversation_id, exc_info=True)
                await asyncio.to_thread(self._stamp, conn, [r["id"] for r in rows], "read_at")
                since = 0.0
            else:
                return  # typing alone also marks read on WhatsApp; nothing to wait for
        wait = self.presence.typing_after_read_s - float(since)
        if wait > 0:
            await asyncio.sleep(wait)

    async def _typing(self, to: str, message_id: str, key: uuid.UUID) -> None:
        try:  # cosmetic: never let it break a turn
            await self.channel.typing(to, message_id)
        except Exception:
            log.warning("typing indicator failed for %s", key, exc_info=True)

    async def _keep_typing(self, to: str, message_id: str, key: uuid.UUID) -> None:
        """Re-send "typing…" while a slow turn is still running (channels drop it after a while)."""
        every = self.presence.typing_refresh_s or 0
        while every > 0:
            await asyncio.sleep(every)
            await self._typing(to, message_id, key)

    def _stamp(self, conn: Connection, ids: list, column: str) -> None:
        i = self.t.inbox
        conn.execute(
            update(i).where(i.c.id.in_(ids), i.c[column].is_(None)).values({column: _clock()})
        )

    def _mark_processed(self, conn: Connection, ids: list, error: str | None = None) -> None:
        i = self.t.inbox
        conn.execute(update(i).where(i.c.id.in_(ids)).values(processed_at=_clock(), error=error))

    def _record_failure(
        self,
        turn: Turn,
        ids: list,
        exc: BaseException,
        ctx: TurnContext,
        started_at: datetime,
    ) -> None:
        i = self.t.inbox
        with self.engine.begin() as conn:
            attempts = conn.scalars(
                update(i)
                .where(i.c.id.in_(ids))
                .values(attempts=i.c.attempts + 1, error=repr(exc)[:500])
                .returning(i.c.attempts)
            ).all()
            gave_up = bool(attempts) and max(attempts) >= self.cfg.max_turn_attempts
            replies: Sequence[Outbound] = []
            if gave_up:
                self._mark_processed(conn, ids, error=f"gave up: {exc!r}"[:500])
                if self.on_failure:
                    replies = self.on_failure(turn, exc)
                    self.enqueue(
                        conn,
                        channel=turn.channel,
                        conversation_id=turn.conversation_id,
                        from_number=turn.to,
                        to=turn.sender.value,
                        recipient_identity_id=turn.sender_identity_id,
                        messages=replies,
                        turn_id=ctx.turn_id,
                    )
            # A failed turn is recorded too: the model calls it made still cost money.
            write_turn(
                conn,
                self.t,
                ctx,
                channel=turn.channel,
                started_at=started_at,
                status="gave_up" if gave_up else "failed",
                messages_in=len(ids),
                messages_out=len(replies),
                error=repr(exc),
            )

    # --- sender ---------------------------------------------------------------------------

    def _lock_next(self, conn: Connection) -> dict[str, Any] | None:
        # The head of each conversation's queue only, so bubbles go out in order; SKIP LOCKED
        # lets several senders work on different conversations at once.
        row = (
            conn.execute(
                text(
                    f"""
                SELECT o.* FROM {self.t.outbox.name} o
                JOIN (SELECT DISTINCT ON (conversation_id) id FROM {self.t.outbox.name}
                      WHERE status = 'pending' ORDER BY conversation_id, id) head
                  ON head.id = o.id
                WHERE o.next_attempt_at <= now()
                ORDER BY o.next_attempt_at
                LIMIT 1
                FOR UPDATE OF o SKIP LOCKED
                """
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def _save_attempt(self, conn: Connection, row_id: Any, values: dict[str, Any]) -> None:
        o = self.t.outbox
        conn.execute(update(o).where(o.c.id == row_id).values(**values))
        conn.commit()

    async def send_once(self) -> int:
        """Send due messages (head of each conversation). Returns how many were attempted."""
        attempted = 0
        while attempted < self.cfg.batch:
            conn = await asyncio.to_thread(self.engine.connect)
            try:
                conn.begin()
                row = await asyncio.to_thread(self._lock_next, conn)
                if row is None:
                    conn.rollback()
                    return attempted
                attempted += 1
                values = await self._attempt(row)
                await asyncio.to_thread(self._save_attempt, conn, row["id"], values)
            finally:
                await asyncio.to_thread(conn.close)
        return attempted

    async def _attempt(self, row: dict[str, Any]) -> dict[str, Any]:
        attempts = row["attempts"] + 1
        base = {"attempts": attempts, "updated_at": _clock()}
        try:
            wamid = await self.channel.send(
                row["from_number"], row["to"], Outbound.from_payload(row["message"])
            )
        except SendError as e:
            if not e.retryable or attempts >= self.cfg.max_send_attempts:
                log.warning("dead letter %s: %s", row["id"], e.reason)
                return base | {"status": "dead", "last_error": e.reason}
            wait = self._backoff(attempts, e.min_wait_s)
            return base | {
                "last_error": e.reason,
                "next_attempt_at": _clock() + timedelta(seconds=wait),
            }
        return base | {
            "status": "sent",
            "channel_message_id": wamid,
            "sent_at": _clock(),
            "last_error": None,
        }

    @staticmethod
    def _backoff(attempt: int, min_wait_s: float = 0) -> float:
        # Full jitter on 2^n, capped at a minute; channel floors (e.g. WhatsApp pair rate
        # limit) grow as min_wait * 4^(n-1), as Meta recommends.
        jittered = random.uniform(0, min(60.0, 2.0**attempt))
        floor = min_wait_s * 4 ** (attempt - 1) if min_wait_s else 0
        return max(jittered, floor, 0.5)

    # --- loops ----------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Reader, worker and sender loops until `stop` is set. Run as a background task."""

        async def loop(step: Callable[[], Awaitable[int]], name: str) -> None:
            while not stop.is_set():
                try:
                    if await step():
                        continue  # more may be waiting
                except Exception:
                    log.exception("%s loop error", name)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.cfg.poll_s)

        await asyncio.gather(
            loop(self.read_once, "reader"),
            loop(self.work_once, "worker"),
            loop(self.send_once, "sender"),
        )


def transcript(
    conn: Connection, t: RuntimeTables, conversation_id: uuid.UUID, limit: int = 30
) -> list[Said]:
    """The last `limit` things said in a conversation, oldest first.

    Inbound: messages already consumed by a turn. Outbound: everything queued except dead
    letters. Both sides are ordered on the database clock: a sender's device clock (the
    channel timestamp) must not interleave with ours."""
    i, o = t.inbox, t.outbox
    ins = conn.execute(
        select(i.c.received_at, i.c.id, i.c.sender_identity_id, i.c.text)
        .where(i.c.conversation_id == conversation_id, i.c.processed_at.is_not(None))
        .order_by(i.c.received_at.desc(), i.c.id.desc())
        .limit(limit)
    ).all()
    outs = conn.execute(
        select(o.c.created_at, o.c.id, o.c.recipient_identity_id, o.c.message, o.c.turn_id)
        .where(o.c.conversation_id == conversation_id, o.c.status != "dead")
        .order_by(o.c.created_at.desc(), o.c.id.desc())
        .limit(limit)
    ).all()
    lines = [(at, id_, Said(at, "in", who, text or "")) for at, id_, who, text in ins]
    lines += [
        (at, id_, Said(at, "out", who, (msg or {}).get("text") or "", turn))
        for at, id_, who, msg, turn in outs
    ]
    return [said for _, _, said in sorted(lines, key=lambda x: (x[0], x[1]))[-limit:]]


def _turn_from_rows(key: uuid.UUID, rows: list[dict[str, Any]]) -> Turn:
    msgs = [
        Inbound(
            channel=r["channel"],
            message_id=r["message_id"],
            to=Address(r["to_scheme"], r["to"], r["to_scope"]),
            sender=Address(r["sender_scheme"], r["sender"], r["sender_scope"]),
            sent_at=r["sent_at"],
            kind=r["kind"],
            text=r["text"],
            payload=r["payload"] or {},
            sender_name=r["sender_name"],
        )
        for r in rows
    ]
    last = rows[-1]  # answer the address that wrote last (it may have changed mid-burst)
    name = next((r["sender_name"] for r in reversed(rows) if r["sender_name"]), None)
    return Turn(
        conversation_id=key,
        channel=last["channel"],
        to=last["to"],
        sender=msgs[-1].sender,
        sender_identity_id=last["sender_identity_id"],
        actor_id=last["actor_id"],
        sender_name=name,
        messages=msgs,
    )
