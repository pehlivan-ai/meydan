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
- `handler(agent, ...)` is an agent as a turn handler: what every app wrote around its agent
  (three apps, three copies), with what was learned doing it built in:

      reply = handler(agent, engine=engine, tables=tables, clock=clock,
                      deps=lambda turn: Deps(turn), now=lambda turn: ["Today's lessons: ..."])
      runtime = Runtime(engine, tables, channel, reply)

  It reads the conversation's transcript as history, runs the agent with a time limit, records
  the turn's model calls (`meydan.observe`), and sends the reply's bubbles (`meydan.reply`).
  What is true only now (the time, today's facts) goes at the very end of the new message, never
  into the instructions: a provider reuses a request up to the first place where it differs from
  the one before (prompt caching), and the instructions come before the whole conversation.
  Measured: with the time in the instructions, a turn's first call read 384 tokens from the
  cache; with it at the end, about 1,536. Tell the model about that block with `NOW_NOTE`.

Requires the `llm` extra (and `runtime`, for `handler`).
"""

import asyncio
import functools
import inspect
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from meydan.channels.base import Outbound
from meydan.clock import Clock
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


# For the agent's instructions: what the block at the end of each message is.
NOW_NOTE = (
    "Each of the person's messages ends with a block in parentheses that the system adds: the "
    "time now, and what is true at this moment. They did not write it; never mention or repeat "
    "it as such. It is always current: when it differs from something said earlier in the "
    "conversation, it is right and the earlier message is out of date."
)
NO_TEXT = "(a message with no text: a photo, a voice note)"


class AgentHandler:
    """An agent as a turn handler. Built by `handler`; call it with a turn, as the runtime does.

    For tests and evals, `run(turn, history)` runs the agent without the database, and
    `prompt(turn)` and `messages(output, turn)` are what a turn is given and what it sends."""

    def __init__(
        self,
        agent: Any,
        *,
        engine: Any = None,
        tables: Any = None,
        deps: Callable[[Any], Any] = lambda turn: turn,
        clock: Clock | None = None,
        now: Callable[[Any], Sequence[str]] | None = None,
        messages: Callable[[Any, Any], list[Outbound]] | None = None,
        history_lines: int = 30,
        timeout_s: float = 45,
        since: Callable[[Any], datetime | None] | None = None,
    ):
        self.agent = agent
        self.engine, self.tables = engine, tables
        self.deps, self.clock, self.now = deps, clock, now
        self._messages = messages
        self.history_lines, self.timeout_s, self.since = history_lines, timeout_s, since

    def prompt(self, turn: Any) -> str:
        """What the model reads for this turn: what the person wrote, then what is true now."""
        lines = []
        if self.clock is not None:
            lines.append(f"Now ({self.clock.zone}): {self.clock.format_datetime(self.clock.now())}")
        if self.now is not None:
            lines.extend(self.now(turn))
        text = turn.text or NO_TEXT
        return f"{text}\n\n(" + "\n".join(lines) + ")" if lines else text

    def messages(self, output: Any, turn: Any) -> list[Outbound]:
        """What a reply sends: its bubbles, or what the app makes of its output."""
        if self._messages is not None:
            return self._messages(output, turn)
        return output.outbound()

    def history(self, turn: Any) -> list[ModelMessage]:
        """The conversation so far, as the model is given it. None without a database."""
        if self.engine is None:
            return []
        from meydan.runtime.conversation import transcript  # noqa: PLC0415

        with self.engine.connect() as conn:
            lines = transcript(conn, self.tables, turn.conversation_id, self.history_lines)
        start = self.since(turn) if self.since is not None else None
        return history([line for line in lines if start is None or line.at > start])

    async def run(self, turn: Any, history: Sequence[ModelMessage] = ()) -> Any:
        """One run of the agent for this turn, with a time limit; records its model calls."""
        from meydan import observe  # noqa: PLC0415

        async with asyncio.timeout(self.timeout_s):
            result = await self.agent.run(
                self.prompt(turn), deps=self.deps(turn), message_history=list(history)
            )
        observe.record_run(result)
        return result

    async def __call__(self, turn: Any) -> list[Outbound]:
        past = await asyncio.to_thread(self.history, turn)
        result = await self.run(turn, past)
        return self.messages(result.output, turn)


def handler(agent: Any, **options: Any) -> AgentHandler:
    """`agent` (a Pydantic AI agent whose output is a `meydan.reply.Reply`) as a turn handler.

    Options: `engine` and `tables` (history from the transcript; left out: none), `deps(turn)`
    (what the agent's tools are given; left out: the turn), `clock` (the time now, at the end of
    each message), `now(turn)` (more lines that are true only now, read on every turn),
    `messages(output, turn)` (what a reply sends; left out: its bubbles), `history_lines`,
    `timeout_s`, `since(turn)` (no history before this moment)."""
    return AgentHandler(agent, **options)
