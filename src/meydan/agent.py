"""Pydantic AI adapter: meydan's tools and a conversation's transcript, as an agent needs them.

meydan stays framework-agnostic: a tool is a plain function (`meydan.tools`), a conversation
is rows (`meydan.runtime.transcript`). This module is the one place that knows Pydantic AI:

    agent = Agent(model, deps_type=Deps, tools=[as_tool(book, context), as_tool(wait, context)])
    result = await agent.run(turn.text, deps=deps, message_history=history(lines))

- `as_tool(fn, context)` exposes a `(ToolContext, **params) -> result` tool. `context(run_ctx)`
  is a context manager that yields the `ToolContext` for one call: the app opens whatever the
  tool needs there (a session, a transaction) and closes it after. Who the tool acts for comes
  from that context, never from an argument the model supplied.
- A `ToolError` (a business rule) becomes `{"error": "..."}`, which the model can explain. The
  context manager still sees the exception, so it can roll back.
- `history(lines)` turns a transcript into model messages: what the person said as requests,
  what we sent as responses, consecutive lines of one side merged, starting with the person.

Requires the `llm` extra.
"""

import functools
import inspect
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from meydan.runtime.conversation import Said
from meydan.tools import ToolContext, ToolError

Context = Callable[[RunContext[Any]], AbstractContextManager[ToolContext]]


def as_tool(fn: Callable[..., Any], context: Context) -> Callable[..., Any]:
    """`fn(ctx: ToolContext, **params)` as a Pydantic AI tool. Its name, docstring and
    parameters are what the model sees; the first parameter is replaced by the run context."""
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if not params:
        raise TypeError(f"{fn.__name__} takes no ToolContext: a tool is fn(ctx, **params)")

    @functools.wraps(fn)
    def tool(ctx: RunContext[Any], **kwargs: Any) -> Any:
        try:
            with context(ctx) as tool_ctx:
                return fn(tool_ctx, **kwargs)
        except ToolError as e:
            return {"error": str(e)}

    first = params[0].replace(annotation=RunContext[Any])
    tool.__signature__ = sig.replace(parameters=[first, *params[1:]])  # type: ignore[attr-defined]
    tool.__annotations__ = {**fn.__annotations__, params[0].name: RunContext[Any]}
    return tool


def history(lines: Iterable[Said]) -> list[ModelMessage]:
    """A transcript (oldest first, text only) as the messages a model is given before a turn."""
    messages: list[ModelMessage] = []
    for line in lines:
        if not line.text:
            continue
        last = messages[-1] if messages else None
        if line.direction == "in":
            if isinstance(last, ModelRequest):
                last.parts[0].content += "\n" + line.text  # type: ignore[union-attr,operator]
            else:
                messages.append(ModelRequest(parts=[UserPromptPart(content=line.text)]))
        elif isinstance(last, ModelResponse):
            last.parts[0].content += "\n" + line.text  # type: ignore[union-attr]
        else:
            messages.append(ModelResponse(parts=[TextPart(content=line.text)]))
    while messages and isinstance(messages[0], ModelResponse):
        messages.pop(0)  # a history starts with the person speaking
    return messages
