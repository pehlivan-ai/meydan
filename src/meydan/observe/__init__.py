"""Observability: what each turn did, how long it took and what it cost (docs/observe.md).

The runtime records one row per turn and one per model call, in the same transaction as the reply.
Your agent code reports model usage with one line, inside the turn:

    result = await agent.run(...)
    observe.record_run(result)

Deep traces (spans per model call and tool call) are a separate, optional OpenTelemetry export.
"""

from meydan.observe.context import TurnContext, current_turn, record_usage, turn_scope
from meydan.observe.usage import Usage, record_run, usage_from_messages

__all__ = [
    "TurnContext",
    "Usage",
    "current_turn",
    "record_run",
    "record_usage",
    "turn_scope",
    "usage_from_messages",
]
