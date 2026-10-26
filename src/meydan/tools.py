"""The tool contract: plain functions an agent can call, independent of any agent framework.

A tool is `def tool(ctx: ToolContext, **params) -> pydantic model`. Framework adapters wrap
tools; they never reimplement them. Docstrings and parameter docs are what the model sees.

Identity comes from `ctx.caller` (e.g. the sender's phone number from the channel), never
from a model-supplied argument, so a person can only act on their own data.

Business-rule failures raise `ToolError`; adapters turn it into a result the model can
explain ("partner already registered"), not a crash.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class ToolContext:
    caller: str  # channel identity of the person the agent acts for
    session: Any = None  # e.g. a SQLAlchemy Session; one per tool call, not shared


class ToolError(Exception):
    """A business-rule failure the model should explain to the person."""
