"""The conversation runtime: inbox → actor per conversation → outbox → sender.

Patterns (docs/architecture.md): Idempotent Receiver, Single Writer per conversation (Postgres
advisory lock), Aggregator (debounce bursts into one turn), Resequencer (order by the sender's
timestamp), Transactional Outbox, Rate-limited sender with jittered retries and a dead letter.

Delivery is at least once end to end: if the process dies after the channel accepted a message
but before we recorded it, that message is sent again. Channels offer no idempotency key.
"""

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import Connection, Engine, case, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from meydan.channels.base import Inbound, Outbound, SendError, StatusUpdate
from meydan.runtime.tables import RuntimeTables

log = logging.getLogger("meydan.runtime")


class Channel(Protocol):
    async def send(self, from_number: str, to: str, message: Outbound) -> str: ...
    async def typing(self, from_number: str, message_id: str) -> None: ...


@dataclass(frozen=True)
class Turn:
    """Everything a person sent since the last turn, in the order they sent it."""

    conversation_key: str
    channel: str
    to: str  # our number
    sender: str
    sender_name: str | None
    messages: Sequence[Inbound]

    @property
    def text(self) -> str:
        return "\n".join(m.text for m in self.messages if m.text)


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
    ):
        self.engine = engine
        self.t = tables
        self.channel = channel
        self.handler = handler
        self.cfg = config or RuntimeConfig()
        self.on_failure = on_failure

    # --- receiving (call from the webhook, inside the request's transaction) -------------

    def receive(
        self, conn: Connection, messages: Sequence[Inbound], statuses: Sequence[StatusUpdate]
    ) -> int:
        """Store new messages (duplicates are ignored) and apply delivery receipts.

        Returns how many messages were new. Cheap: safe to call before answering 200.
        """
        new = 0
        for m in messages:
            row = {
                "channel": m.channel,
                "message_id": m.message_id,
                "conversation_key": m.conversation_key,
                "to": m.to,
                "sender": m.sender,
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
            new += len(conn.execute(stmt).all())  # rowcount is unreliable with ON CONFLICT
        for s in statuses:
            self._apply_status(conn, s)
        return new

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
            update(o).where(cond).values(status=s.status, last_error=s.error, updated_at=func.now())
        )

    # --- enqueueing (replies, and proactive messages from planners) -----------------------

    def enqueue(
        self,
        conn: Connection,
        *,
        channel: str,
        conversation_key: str,
        from_number: str,
        to: str,
        messages: Sequence[Outbound],
        start_at: datetime | None = None,
    ) -> None:
        """Queue messages for one conversation, paced like someone typing them one by one."""
        at = start_at or datetime.now(UTC)
        rows = []
        for i, m in enumerate(messages):
            if i:
                gap = len(m.text or "") / self.cfg.chars_per_second
                at += timedelta(seconds=min(gap, self.cfg.max_bubble_gap_s))
            rows.append(
                {
                    "channel": channel,
                    "conversation_key": conversation_key,
                    "from_number": from_number,
                    "to": to,
                    "message": m.to_payload(),
                    "next_attempt_at": at,
                }
            )
        if rows:
            conn.execute(insert(self.t.outbox), rows)

    # --- worker: one turn per conversation at a time ---------------------------------------

    def _due_conversations(self) -> list[str]:
        i = self.t.inbox
        now = func.now()
        q = (
            select(i.c.conversation_key)
            .where(i.c.processed_at.is_(None))
            .group_by(i.c.conversation_key)
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

    def _claim(self, conn: Connection, key: str) -> list[dict[str, Any]] | None:
        got = conn.scalar(select(func.pg_try_advisory_xact_lock(func.hashtextextended(key, 0))))
        if not got:
            return None  # another worker is in a turn for this conversation
        i = self.t.inbox
        rows = conn.execute(
            select(i)
            .where(i.c.conversation_key == key, i.c.processed_at.is_(None))
            .order_by(i.c.sent_at, i.c.id)  # Resequencer: the person's order, not arrival
        ).mappings()
        return [dict(r) for r in rows]

    async def _turn(self, key: str) -> int:
        conn = await asyncio.to_thread(self.engine.connect)
        try:
            trans = conn.begin()
            rows = await asyncio.to_thread(self._claim, conn, key)
            if not rows:
                trans.rollback()
                return 0
            turn = _turn_from_rows(key, rows)
            ids = [r["id"] for r in rows]
            await self.channel.typing(turn.to, rows[-1]["message_id"])
            try:
                replies = await self.handler(turn)
            except Exception as e:
                trans.rollback()
                log.exception("turn failed for %s", _mask(key))
                await asyncio.to_thread(self._record_failure, turn, ids, e)
                return 0

            def commit() -> None:
                self.enqueue(
                    conn,
                    channel=turn.channel,
                    conversation_key=key,
                    from_number=turn.to,
                    to=turn.sender,
                    messages=replies,
                )
                self._mark_processed(conn, ids)
                trans.commit()

            await asyncio.to_thread(commit)
            return 1
        finally:
            await asyncio.to_thread(conn.close)

    def _mark_processed(self, conn: Connection, ids: list, error: str | None = None) -> None:
        i = self.t.inbox
        conn.execute(update(i).where(i.c.id.in_(ids)).values(processed_at=func.now(), error=error))

    def _record_failure(self, turn: Turn, ids: list, exc: BaseException) -> None:
        i = self.t.inbox
        with self.engine.begin() as conn:
            attempts = conn.scalars(
                update(i)
                .where(i.c.id.in_(ids))
                .values(attempts=i.c.attempts + 1, error=repr(exc)[:500])
                .returning(i.c.attempts)
            ).all()
            if attempts and max(attempts) >= self.cfg.max_turn_attempts:
                self._mark_processed(conn, ids, error=f"gave up: {exc!r}"[:500])
                if self.on_failure:
                    self.enqueue(
                        conn,
                        channel=turn.channel,
                        conversation_key=turn.conversation_key,
                        from_number=turn.to,
                        to=turn.sender,
                        messages=self.on_failure(turn, exc),
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
                JOIN (SELECT DISTINCT ON (conversation_key) id FROM {self.t.outbox.name}
                      WHERE status = 'pending' ORDER BY conversation_key, id) head
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
        base = {"attempts": attempts, "updated_at": func.now()}
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
                "next_attempt_at": datetime.now(UTC) + timedelta(seconds=wait),
            }
        return base | {
            "status": "sent",
            "channel_message_id": wamid,
            "sent_at": func.now(),
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
        """Worker and sender loops until `stop` is set. Run as a background task."""

        async def loop(step: Callable[[], Awaitable[int]], name: str) -> None:
            while not stop.is_set():
                try:
                    if await step():
                        continue  # more may be waiting
                except Exception:
                    log.exception("%s loop error", name)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.cfg.poll_s)

        await asyncio.gather(loop(self.work_once, "worker"), loop(self.send_once, "sender"))


def _turn_from_rows(key: str, rows: list[dict[str, Any]]) -> Turn:
    first = rows[0]
    msgs = [
        Inbound(
            channel=r["channel"],
            message_id=r["message_id"],
            to=r["to"],
            sender=r["sender"],
            sent_at=r["sent_at"],
            kind=r["kind"],
            text=r["text"],
            payload=r["payload"] or {},
            sender_name=r["sender_name"],
        )
        for r in rows
    ]
    name = next((r["sender_name"] for r in reversed(rows) if r["sender_name"]), None)
    return Turn(key, first["channel"], first["to"], first["sender"], name, msgs)


def _mask(key: str) -> str:
    """Conversation keys contain phone numbers: logs show only the last 4 digits."""
    return f"{key.rsplit(':', 1)[0]}:***{key[-4:]}"
