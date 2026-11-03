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
from datetime import datetime

from sqlalchemy import Connection, Engine

from meydan.channels.base import Inbound, Outbound, StatusUpdate, TemplateUpdate
from meydan.runtime.conversation import Said, transcript
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
    RuntimeConfig,
    Turn,
)
from meydan.runtime.worker import Worker

__all__ = [
    "Channel", "FailureReply", "Handler", "Intercepted", "Interceptor", "Replied", "Runtime",
    "RuntimeConfig", "Said", "Turn", "replied_to", "transcript",
]  # fmt: skip

log = logging.getLogger("meydan.runtime")


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
    ) -> uuid.UUID | None:
        """See `Outbox.enqueue`."""
        return self._outbox.enqueue(
            conn, channel=channel, conversation_id=conversation_id, from_number=from_number,
            to=to, messages=messages, recipient_identity_id=recipient_identity_id,
            start_at=start_at, turn_id=turn_id,
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
