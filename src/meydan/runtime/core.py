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
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import Connection, Engine, func, select

from meydan.channels.base import Inbound, Outbound, StatusUpdate, TemplateUpdate
from meydan.runtime.conversation import Said, transcript

if TYPE_CHECKING:
    from meydan.media import MediaStore
    from meydan.runtime.expect import Expectations
from meydan.runtime.inbound import Receiver, Replied, replied_to
from meydan.runtime.outbound import Outbox, backoff
from meydan.runtime.presence import Presence
from meydan.runtime.tables import RuntimeTables
from meydan.runtime.types import (
    Channel,
    FailureReply,
    Handler,
    Intercepted,
    Interceptor,
    Observer,
    RuntimeConfig,
    Turn,
    _clock,
)
from meydan.runtime.worker import Worker, taken_over_now

__all__ = [
    "Channel", "FailureReply", "Handler", "Intercepted", "Interceptor", "Observer", "Replied",
    "Runtime", "RuntimeConfig", "Said", "Turn", "replied_to", "transcript",
]  # fmt: skip
from meydan.rules import DEFAULT, Rules

log = logging.getLogger("meydan.runtime")


@dataclass(frozen=True)
class Health:
    """Whether due work is being done: the oldest message waiting for its turn, the oldest reply
    waiting to be sent (seconds), and replies given up in the last hour (shown, not judged)."""

    ok: bool
    waiting_s: float
    sending_s: float
    dead_last_hour: int


class Runtime:
    """The runtime's one face: receive (inbound), enqueue and send (outbound), read and turn
    (worker), transcript (conversation). The parts share this object's state."""

    def __init__(
        self,
        engine: Engine,
        tables: RuntimeTables,
        channel: Channel,
        handler: Handler,
        config: RuntimeConfig | None = None,
        on_failure: FailureReply | None = None,
        on_delay: FailureReply | None = None,
        rules: "Rules | None" = None,
        intercept: Sequence[Interceptor] = (),
        expect: "Expectations | None" = None,
        on_receive: Sequence[Observer] = (),
        media_store: "MediaStore | None" = None,
    ):
        self.engine = engine
        self.t = tables
        self.channel = channel
        # Which channel's turns and replies this runtime takes (see `Channel.name`).
        self.serves: str | None = getattr(channel, "name", None)
        if self.serves is None:
            log.warning("the channel has no name: this runtime takes every channel's turns")
        self.handler = handler
        self.cfg = config or RuntimeConfig()
        self.presence = Presence.of(self.cfg.presence)
        # Answers to what we asked are matched before anything else sees them.
        self.expect = expect
        self.intercept = ([expect.intercept] if expect else []) + list(intercept)
        # Told of every new message, in its transaction, before interceptors (a new person, say).
        self.on_receive = list(on_receive)
        # Where the bytes of the files we send live (meydan.media); Postgres unless given.
        from meydan.media import PostgresStore

        self.media_store = media_store or PostgresStore(tables)
        self.on_failure = on_failure  # the turn is given up
        self.on_delay = on_delay  # the turn is put off for the first time
        self.rules = rules or DEFAULT  # the channels' rules (`meydan.rules`)
        self._receiver = Receiver(self)
        self._outbox = Outbox(self)
        self._worker = Worker(self)

    def receive(
        self,
        conn: Connection,
        messages: Sequence[Inbound],
        statuses: Sequence[StatusUpdate],
        template_updates: Sequence[TemplateUpdate] = (),
    ) -> int:
        """See `Receiver.receive`."""
        return self._receiver.receive(conn, messages, statuses, template_updates)

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
        author: str = "system",
        author_actor_id: uuid.UUID | None = None,
    ) -> uuid.UUID | None:
        """See `Outbox.enqueue`."""
        return self._outbox.enqueue(
            conn, channel=channel, conversation_id=conversation_id, from_number=from_number,
            to=to, messages=messages, recipient_identity_id=recipient_identity_id,
            start_at=start_at, turn_id=turn_id, author=author, author_actor_id=author_actor_id,
        )  # fmt: skip

    def transcript(
        self, conn: Connection, conversation_id: uuid.UUID, limit: int = 30
    ) -> list[Said]:
        """See `transcript`."""
        return transcript(conn, self.t, conversation_id, limit)

    async def read_once(self) -> int:
        """See `Worker.read_once`."""
        return await self._worker.read_once()

    async def work_once(self) -> int:
        """See `Worker.work_once`."""
        return await self._worker.work_once()

    async def send_once(self) -> int:
        """See `Outbox.send_once`."""
        return await self._outbox.send_once()

    _backoff = staticmethod(backoff)

    def health(self, max_waiting_s: float = 120, max_sending_s: float = 300) -> "Health":
        """Is work that is due being done? Read from the tables, so it says the truth even when
        the loops that should do the work are stuck. A turn put off on purpose is not waiting
        (`retry_at` ahead); a message is waiting once its turn is due and nobody took it."""
        i, o = self.t.inbox, self.t.outbox
        now = _clock()
        ours_in = [i.c.channel == self.serves] if self.serves else []
        ours_out = [o.c.channel == self.serves] if self.serves else []
        due = (i.c.retry_at.is_(None)) | (i.c.retry_at <= now)
        # A conversation a person has taken over waits on purpose: not stuck work.
        due = due & ~taken_over_now(self.t, i.c.conversation_id)
        with self.engine.connect() as conn:
            waiting = conn.scalar(
                select(func.extract("epoch", now - func.min(i.c.received_at))).where(
                    i.c.processed_at.is_(None), due, *ours_in
                )
            )
            sending = conn.scalar(
                select(func.extract("epoch", now - func.min(o.c.next_attempt_at))).where(
                    o.c.status == "pending", o.c.next_attempt_at <= now, *ours_out
                )
            )
            dead = conn.scalar(
                select(func.count()).where(
                    o.c.status == "dead", o.c.updated_at >= now - timedelta(hours=1), *ours_out
                )
            )
        waiting_s, sending_s = float(waiting or 0), float(sending or 0)
        return Health(
            ok=waiting_s <= max_waiting_s and sending_s <= max_sending_s,
            waiting_s=round(waiting_s, 1),
            sending_s=round(sending_s, 1),
            dead_last_hour=int(dead or 0),
        )

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

        async def sweep() -> None:  # close overdue expectations, at a slower pace
            while self.expect is not None and not stop.is_set():
                try:
                    await asyncio.to_thread(self.expect.sweep_once, self.engine)
                except Exception:
                    log.exception("expectations sweep error")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.cfg.expect_sweep_s)

        async def forget() -> None:  # received files past the app's retention, hourly
            while self.cfg.keep_received is not None and not stop.is_set():
                try:
                    await asyncio.to_thread(self.forget_received_once)
                except Exception:
                    log.exception("received media sweep error")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.cfg.media_sweep_s)

        await asyncio.gather(
            loop(self.read_once, "reader"),
            loop(self.work_once, "worker"),
            loop(self.send_once, "sender"),
            sweep(),
            forget(),
        )

    def forget_received_once(self) -> int:
        """Drop the received files older than `RuntimeConfig.keep_received` (meydan.media)."""
        from meydan import media  # noqa: PLC0415

        if self.cfg.keep_received is None:
            return 0
        with self.engine.begin() as conn:
            n = media.forget_received(conn, self.t, self.cfg.keep_received, store=self.media_store)
        if n:
            log.info("forgot %d received files", n)
        return n
