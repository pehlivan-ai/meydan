"""Runtime types: what a channel, a turn, an interceptor and the config look like."""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import Connection, func

from meydan.channels.base import Address, Inbound, Outbound
from meydan.runtime.presence import Presence

if TYPE_CHECKING:
    from meydan.runtime import identity
    from meydan.runtime.core import Runtime


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


class _Part:
    """A piece of the runtime: reads the shared state (tables, engine, channel, config) from the
    Runtime it belongs to, so the Runtime stays the one place it lives."""

    def __init__(self, rt: "Runtime"):
        self.rt = rt

    t = property(lambda self: self.rt.t)
    engine = property(lambda self: self.rt.engine)
    channel = property(lambda self: self.rt.channel)
    cfg = property(lambda self: self.rt.cfg)
    presence = property(lambda self: self.rt.presence)
    handler = property(lambda self: self.rt.handler)
    on_failure = property(lambda self: self.rt.on_failure)
    intercept = property(lambda self: self.rt.intercept)

    def enqueue(self, conn: Connection, **kwargs: Any) -> uuid.UUID | None:
        return self.rt.enqueue(conn, **kwargs)
