"""Runtime types: what a channel, a turn, an interceptor and the config look like."""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
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
    # Optional: the channel's name ("whatsapp", "telegram"), as its messages carry it. A runtime
    # takes only that channel's turns and sends only its replies, so two apps (or a test with a
    # fake) on one database never take each other's. Without a name a runtime takes every turn.
    # name: str

    async def send(self, from_number: str, to: str, message: Outbound) -> str: ...

    # Mark the message (and everything before it) read and show "typing…".
    async def typing(self, from_number: str, message_id: str) -> None: ...

    # Optional: mark read without typing. Channels without it get no separate read receipts.
    # async def mark_read(self, from_number: str, message_id: str) -> None: ...

    # Optional: the file someone sent in a message (a photo): (bytes, media type), or None when it
    # carries none. The worker keeps it before the turn (RuntimeConfig.fetch_media); channels
    # without it keep only the text.
    # async def download_media(self, message: Inbound) -> tuple[bytes, str] | None: ...


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
    # What the app's routing found out about the sender (`route_by`): a role, a profile, a
    # credential. None unless a resolver set it; meydan never reads it.
    resolved: Any = None

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
# Told of a new message where it landed (`Routed.new_actor`: how we met them). Runs in the
# receiving transaction: write what should follow (a notification) there, send nothing.
Observer = Callable[[Connection, Inbound, "identity.Routed"], None]
Handler = Callable[[Turn], Awaitable[Sequence[Outbound]]]
# What the person is sent when a turn is given up (`on_failure`) or first put off (`on_delay`).
# The second argument is a `meydan.runtime.failures.Failure`: its `kind`, `group`, `error`.
FailureReply = Callable[[Turn, Any], Sequence[Outbound]]


@dataclass
class RuntimeConfig:
    debounce_s: float = 2.5  # wait this long after a person's last message before replying
    max_wait_s: float = 8.0  # ...but never longer than this after their first unanswered one
    max_turn_attempts: int = 3  # then give up on those messages (poison message guard)
    # A turn that fails for a passing reason (the model's provider is down) is tried again after
    # these waits, the last one repeating. It is given up when the channel no longer lets us
    # answer (its reply window, `meydan.rules`), or after `give_up_after` on a channel without one.
    retry_waits_s: tuple[float, ...] = (30, 120, 600, 1800, 3600)
    retry_wait_s: float = 5  # between tries of a failure nobody names, times the attempt (max 60)
    give_up_after_s: float = 24 * 3600
    max_send_attempts: int = 6  # then dead letter
    chars_per_second: float = 40  # pacing between bubbles ("typing" speed)
    max_bubble_gap_s: float = 3.0
    poll_s: float = 0.5
    batch: int = 10
    # "Seen" and "typing…": a style name ("human", "instant", "quiet") or a Presence with numbers.
    presence: Presence | str = field(default_factory=Presence.human)
    expect_sweep_s: float = 30.0  # how often overdue expectations are closed (Runtime(expect=))
    # A reaction (an emoji on a message) is recorded and read back in the transcript, but opens
    # no turn: nobody answers a 🙏. True: the agent gets a turn for it like any message.
    answer_reactions: bool = False
    # What people send as files, kept before the turn (the channel's `download_media`, then
    # meydan.media.receive) so an agent can see it and the team can open it. Kinds by the
    # channel's name for them; empty: nothing is fetched.
    fetch_media: frozenset[str] = frozenset({"image"})
    image_max_edge: int = 1568  # a received photo's long edge once kept (meydan.media)
    # How long received files are kept; None: until the app deletes them. Swept hourly.
    keep_received: timedelta | None = None
    media_sweep_s: float = 3600.0


_STATUS_RANK = {"pending": 0, "sent": 1, "delivered": 2, "read": 3}


class _Part:
    """A piece of the runtime: reads the shared state (tables, engine, channel, config) from the
    Runtime it belongs to, so the Runtime stays the one place it lives."""

    def __init__(self, rt: "Runtime"):
        self.rt = rt

    t = property(lambda self: self.rt.t)
    engine = property(lambda self: self.rt.engine)
    channel = property(lambda self: self.rt.channel)
    serves = property(lambda self: self.rt.serves)
    cfg = property(lambda self: self.rt.cfg)
    presence = property(lambda self: self.rt.presence)
    handler = property(lambda self: self.rt.handler)
    on_failure = property(lambda self: self.rt.on_failure)
    on_delay = property(lambda self: self.rt.on_delay)
    rules = property(lambda self: self.rt.rules)
    intercept = property(lambda self: self.rt.intercept)
    on_receive = property(lambda self: self.rt.on_receive)

    def enqueue(self, conn: Connection, **kwargs: Any) -> uuid.UUID | None:
        return self.rt.enqueue(conn, **kwargs)
