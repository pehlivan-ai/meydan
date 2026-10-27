"""The tool contract: plain functions an agent can call, independent of any agent framework.

A tool is `def tool(ctx: ToolContext, **params) -> pydantic model`. Framework adapters wrap
tools; they never reimplement them. Docstrings and parameter docs are what the model sees.

Identity comes from `ctx.actor_id` (the actor the runtime settled for this turn, see
docs/identity.md), never from a model-supplied argument, so a person can only act on their own
data, whichever address they wrote from.

Business-rule failures raise `ToolError`; adapters turn it into a result the model can
explain ("partner already registered"), not a crash.
"""

import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolContext:
    actor_id: uuid.UUID  # the actor the agent acts for (Turn.actor_id)
    session: Any = None  # e.g. a SQLAlchemy Session; one per tool call, not shared


class ToolError(Exception):
    """A business-rule failure the model should explain to the person."""
