"""Routing: one handler per kind of sender, chosen in code.

    handler = route_by(resolve, {"staff": staff, "member": member, "guest": guest}, default="guest")
    runtime = Runtime(engine, tables, channel, handler)

`resolve(turn)` says which key a turn belongs to: a role the app looks up for `turn.actor_id`,
the number that was written to (`turn.to`), anything the app can know without asking a model.
meydan does not know what the keys mean.

A handler is still `Turn -> list[Outbound]`, so what stands behind a key is the app's choice: one
agent, several, fixed text, or another `route_by`.

A lookup usually finds more than the key (a name, a profile, a credential for the systems the
tools call). `resolve` may return `(key, found)`: the handler then gets the turn with
`turn.resolved = found`, so nobody is looked up twice.

Three rules, the reason this is a part and not three lines in every app:

- The key is resolved again on every turn, so a change takes effect with the next message.
- No key, or a key nothing is registered for, goes to `default`: say who answers a stranger, and
  give that handler the least it needs.
- If `resolve` fails, the turn fails and is tried again like any other. Someone the app could not
  look up is not answered as if they were a stranger.
"""

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from collections.abc import Sequence as Seq
from dataclasses import replace
from typing import Any

from meydan.channels.base import Outbound
from meydan.runtime.types import Handler, Turn

log = logging.getLogger("meydan.runtime.routing")

# Which key a turn belongs to (None: the app does not know the sender), alone or with what the
# lookup found. Plain or async.
Found = str | None | tuple[str | None, Any]
Resolve = Callable[[Turn], Found | Awaitable[Found]]


def route_by(resolve: Resolve, handlers: Mapping[str, Handler], *, default: str) -> Handler:
    """A handler that passes each turn to the handler registered for the key `resolve` gives."""
    if default not in handlers:
        raise ValueError(f"default {default!r} has no handler; registered: {sorted(handlers)}")
    handlers = dict(handlers)

    async def routed(turn: Turn) -> Seq[Outbound]:
        key = resolve(turn)
        if inspect.isawaitable(key):
            key = await key
        if isinstance(key, tuple):
            key, found = key
            turn = replace(turn, resolved=found)
        if key is not None and key not in handlers:
            log.warning("no handler for %r: turn goes to %r", key, default)
        return await handlers[key if key in handlers else default](turn)

    return routed
