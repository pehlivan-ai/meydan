"""Scenarios for an agent, with Pydantic Evals: meydan gives the task and a few evaluators.

Pydantic Evals runs the cases, repeats them, runs them in parallel and prints the report
(`pydantic_evals.Dataset`). What meydan adds is what only meydan knows:

- `talk(handler, says)` runs a conversation the way the runtime would: the same handler, the same
  prompt with what is true now at the end, the same messages sent. The eval tests the real
  thing, not a copy.
- It counts what the model was told it got wrong and sent again (a tool's arguments, a reply out
  of shape): the first sign of a tool shaped badly for a model, before a turn fails.
- Evaluators for what an agent did: `Called`, `NoCalls`, `Says`, `DoesNotSay`, `MaxBubbles`,
  and the score `SentBack`.

    from pydantic_evals import Case, Dataset
    from meydan.eval import Called, Says, SentBack, conversation

    dataset = Dataset(
        name="lessons",
        cases=[Case(name="tomorrow", inputs=["what is on tomorrow"],
                    evaluators=[Called("lessons", {"day": "2026-11-17"}), Says("16:00")])],
        evaluators=[SentBack()],
    )
    report = dataset.evaluate_sync(conversation(handler, resolved=who), repeat=3)
    report.print(include_reasons=True)

A case is checked from what the agent did and what it sends, never by how well it reads. One run
is a smoke test, not a measurement: repeat. Requires the `eval`, `llm` and `runtime` extras.
"""

import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic_ai import capture_run_messages
from pydantic_ai.messages import RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_evals.evaluators import EvaluationReason, Evaluator, EvaluatorContext

from meydan.agent import AgentHandler
from meydan.channels.base import Address, Inbound
from meydan.runtime import Turn


def sent_back(part: Any) -> bool:
    """A part that told the model its tool call or reply was refused, and why. Pydantic AI says
    it either as a retry prompt or as a tool result marked `retry`, depending on the case."""
    return isinstance(part, RetryPromptPart) or (
        isinstance(part, ToolReturnPart) and bool(getattr(part, "retry", False))
    )


@dataclass
class Conversation:
    """What a conversation did: the task's output, read by the evaluators."""

    sent: list[str]  # the texts the last turn sends
    calls: list[tuple[str, dict]]  # the last turn's tool calls
    output: Any  # the last turn's output (a `meydan.reply.Reply`)
    sent_back: int  # refused and sent back to the model, over the whole conversation
    extra: dict[str, Any] = field(default_factory=dict)  # what an app keeps for its evaluators


class TurnFailedError(Exception):
    """A turn failed: the report shows it as a failure, with what the model sent and what was
    refused."""


def turn(text: str, resolved: Any = None, channel: str = "whatsapp") -> Turn:
    """A turn as the runtime would hand it to a handler: one message, a person nobody knows."""
    sender, us = Address("tel", "+10000000000"), Address("eval", "eval")
    message = Inbound(channel, "eval", us, sender, datetime.now(UTC), "text", text)
    name = getattr(resolved, "name", None)
    return Turn(uuid.uuid4(), channel, "eval", sender, uuid.uuid4(), uuid.uuid4(), name,
                [message], resolved=resolved)  # fmt: skip


async def talk(
    handler: AgentHandler,
    says: Sequence[str],
    *,
    resolved: Any = None,
    before: Callable[[int], None] | None = None,
    channel: str = "whatsapp",
) -> Conversation:
    """`says` one message per turn, each turn given the conversation so far. `before(n)` runs
    before turn n (from 1): a world that changes during the conversation."""
    past: list = []
    with capture_run_messages() as trail:
        try:
            for n, text in enumerate(says, start=1):
                if before is not None:
                    before(n)
                last = turn(text, resolved, channel)
                result = await handler.run(last, past)
                past = result.all_messages()
        except Exception as e:
            lines = [f"{type(e).__name__}: {e}"]
            for m in trail:  # what the model sent and what was refused: the finding itself
                for p in m.parts:
                    if isinstance(p, ToolCallPart):
                        lines.append(f"sent {p.tool_name}({p.args!r})"[:200])
                    elif sent_back(p):
                        why = p.model_response() if isinstance(p, RetryPromptPart) else p.content
                        lines.append(f"refused: {why}"[:200])
            raise TurnFailedError("\n".join(lines)) from e
    return Conversation(
        sent=[m.text for m in handler.messages(result.output, last)],
        calls=[
            (p.tool_name, p.args_as_dict())
            for m in result.new_messages()
            for p in m.parts
            if isinstance(p, ToolCallPart) and p.tool_name != "final_result"
        ],
        output=result.output,
        sent_back=sum(sent_back(p) for m in past for p in m.parts),
    )


def conversation(handler: AgentHandler, **options: Any) -> Callable[..., Any]:
    """A Pydantic Evals task: a case's inputs are the person's messages, one per turn."""

    async def task(says: Sequence[str]) -> Conversation:
        return await talk(handler, says, **options)

    return task


Ctx = EvaluatorContext[Any, Conversation, Any]


@dataclass(repr=False)
class Called(Evaluator):
    """The last turn called `tool`, with these arguments among the ones it gave."""

    tool: str
    args: dict = field(default_factory=dict)

    def evaluate(self, ctx: Ctx) -> EvaluationReason:
        ok = any(
            name == self.tool and all(a.get(k) == v for k, v in self.args.items())
            for name, a in ctx.output.calls
        )
        return EvaluationReason(ok, None if ok else f"calls: {ctx.output.calls}")


@dataclass(repr=False)
class NoCalls(Evaluator):
    """The last turn called no tool."""

    def evaluate(self, ctx: Ctx) -> EvaluationReason:
        calls = [name for name, _ in ctx.output.calls]
        return EvaluationReason(not calls, f"called {calls}" if calls else None)


@dataclass(repr=False)
class Says(Evaluator):
    """What the last turn sends has one of these, in any case."""

    any_of: tuple[str, ...]

    def __init__(self, *any_of: str):
        self.any_of = any_of

    def evaluate(self, ctx: Ctx) -> EvaluationReason:
        text = "\n".join(ctx.output.sent).casefold()
        ok = any(word.casefold() in text for word in self.any_of)
        return EvaluationReason(ok, None if ok else f"none of {self.any_of} in what is sent")


@dataclass(repr=False)
class DoesNotSay(Evaluator):
    """What the last turn sends matches none of these (regular expressions, any case)."""

    patterns: tuple[str, ...]

    def __init__(self, *patterns: str):
        self.patterns = patterns

    def evaluate(self, ctx: Ctx) -> EvaluationReason:
        text = "\n".join(ctx.output.sent)
        found = [m.group(0) for p in self.patterns if (m := re.search(p, text, re.IGNORECASE))]
        return EvaluationReason(not found, f"sends {found}" if found else None)


@dataclass(repr=False)
class MaxBubbles(Evaluator):
    """The model wrote at most `n` bubbles (what the app adds to them is not counted)."""

    n: int = 3

    def evaluate(self, ctx: Ctx) -> EvaluationReason:
        count = len(getattr(ctx.output.output, "bubbles", ctx.output.sent))
        return EvaluationReason(count <= self.n, f"{count} bubbles" if count > self.n else None)


@dataclass(repr=False)
class SentBack(Evaluator):
    """A score, not a pass or fail: how often the model was told it got something wrong and sent
    it again. Above zero across runs, a tool is likely shaped badly for the model."""

    def evaluate(self, ctx: Ctx) -> int:
        return ctx.output.sent_back


def summary(report: Any, verbose: bool = False) -> bool:
    """One line per case for a terminal (pass or fail, seconds, sent back), why a case failed,
    and the count; Pydantic Evals' full table with `verbose`. True when every run passed."""
    if verbose:
        report.print(include_reasons=True)
    runs: dict[str, list] = {}
    for c in report.cases:
        runs.setdefault(c.source_case_name or c.name, []).append(c)
    for f in report.failures:
        runs.setdefault(f.source_case_name or f.name, []).append(f)
    passed = total = 0
    for name, cases in runs.items():
        ok = [c for c in cases if hasattr(c, "assertions")
              and all(a.value for a in c.assertions.values())]  # fmt: skip
        passed, total = passed + len(ok), total + len(cases)
        timed = [c.task_duration for c in cases if hasattr(c, "task_duration")]
        seconds = sum(timed) / len(timed) if timed else 0.0
        back = sum(
            int(c.scores["SentBack"].value) for c in cases if "SentBack" in getattr(c, "scores", {})
        )
        mark = "ok  " if len(ok) == len(cases) else "FAIL"
        print(f"{mark} {len(ok)}/{len(cases)}  {seconds:4.1f}s  {name}"
              + (f"  ({back} sent back)" if back else ""))  # fmt: skip
        for c in cases:
            if not hasattr(c, "assertions"):  # the task itself failed
                print("        - " + str(c.error_message).replace("\n", "\n          "))
                continue
            for label, a in c.assertions.items():
                if not a.value:
                    print(f"        - {label}: {a.reason}")
            if c not in ok:
                for text in c.output.sent:
                    print("        │ " + text.replace("\n", "\n        │ "))
    print(f"\n{passed}/{total} passed")
    return passed == total
