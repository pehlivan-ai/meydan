"""The turn being worked on, carried implicitly from the runtime into your code (contextvars)."""

import contextlib
import uuid
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meydan.observe.usage import Usage


@dataclass
class TurnContext:
    turn_id: uuid.UUID
    conversation_id: uuid.UUID
    actor_id: uuid.UUID | None
    usage: list["Usage"] = field(default_factory=list)


_current: ContextVar[TurnContext | None] = ContextVar("meydan_turn", default=None)


def current_turn() -> TurnContext | None:
    """The turn the runtime is running right now, or None outside a turn."""
    return _current.get()


@contextlib.contextmanager
def turn_scope(ctx: TurnContext) -> Iterator[TurnContext]:
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


def record_usage(*usage: "Usage") -> None:
    """Add model usage to the current turn. Outside a turn (scripts, tests) it does nothing."""
    ctx = _current.get()
    if ctx is not None:
        ctx.usage.extend(usage)
