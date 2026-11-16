"""Scenarios on Pydantic Evals: meydan's task runs the real handler, its evaluators check what
the agent did. No live model: Pydantic AI's function model plays one."""

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_evals import Case, Dataset

from meydan.agent import handler
from meydan.eval import (
    Called,
    DoesNotSay,
    MaxBubbles,
    NoCalls,
    Says,
    SentBack,
    TurnFailedError,
    conversation,
    sent_back,
    summary,
    talk,
)
from meydan.reply import Reply


def lessons(day: str) -> str:
    """The lessons of a day."""
    return {"2026-11-17": "16:00 piano"}.get(day, "none")


def model(*, day: str = "2026-11-17", bad_first: bool = False):
    """Calls `lessons` for `day` (once with arguments it gets wrong, if asked), then answers
    with what came back."""

    def respond(messages, info):
        [output] = info.output_tools
        parts = messages[-1].parts
        back = [p for p in parts if isinstance(p, ToolReturnPart) and not sent_back(p)]
        if back:
            return ModelResponse(parts=[ToolCallPart(output.name, {"bubbles": [back[0].content]})])
        if bad_first and len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("lessons", {"dummy": True})])
        return ModelResponse(parts=[ToolCallPart("lessons", {"day": day})])

    return FunctionModel(respond)


def agent(m) -> object:
    return handler(Agent(m, output_type=Reply, tools=[lessons]))


def assertions(report) -> dict[str, bool]:
    [case] = report.cases
    return {name: a.value for name, a in case.assertions.items()}


async def test_a_case_passes_when_the_agent_does_what_it_should():
    dataset = Dataset(name="t", cases=[Case(name="tomorrow", inputs=["ne var"], evaluators=(
        Called("lessons", {"day": "2026-11-17"}), Says("16:00", "4 pm"), DoesNotSay(r"none"),
        MaxBubbles(1)))], evaluators=[SentBack()])  # fmt: skip
    report = await dataset.evaluate(conversation(agent(model())), progress=False)
    assert all(assertions(report).values()) and report.cases[0].scores["SentBack"].value == 0


async def test_what_does_not_hold_says_why():
    dataset = Dataset(name="t", cases=[Case(name="tomorrow", inputs=["ne var"], evaluators=(
        Called("lessons", {"day": "2026-11-17"}), NoCalls(), Says("16:00")))])  # fmt: skip
    report = await dataset.evaluate(conversation(agent(model(day="2026-11-18"))), progress=False)
    [case] = report.cases
    assert not any(a.value for a in case.assertions.values())
    assert case.assertions["NoCalls"].reason == "called ['lessons']"


async def test_arguments_the_model_got_wrong_are_counted_even_when_the_turn_recovers():
    talked = await talk(agent(model(bad_first=True)), ["ne var"])
    assert talked.sent == ["16:00 piano"] and talked.sent_back == 1


async def test_a_turn_that_fails_says_what_the_model_sent_and_what_was_refused():
    def always_wrong(messages, info):
        return ModelResponse(parts=[ToolCallPart("lessons", {"dummy": True})])

    with pytest.raises(TurnFailedError, match=r"sent lessons\(\{'dummy': True\}\)"):
        await talk(agent(FunctionModel(always_wrong)), ["ne var"])


async def test_before_runs_before_each_turn_and_repeats_come_from_pydantic_evals():
    seen: list[int] = []
    task = conversation(agent(model()), before=seen.append)
    report = await Dataset(name="t", cases=[Case(inputs=["a", "b"])]).evaluate(
        task, repeat=2, progress=False
    )
    assert len(report.cases) == 2 and sorted(seen) == [1, 1, 2, 2]  # repeats run in parallel


async def test_the_summary_is_a_line_per_case_and_says_why_one_failed(capsys):
    def always_wrong(messages, info):
        return ModelResponse(parts=[ToolCallPart("lessons", {"dummy": True})])

    good = Case(name="good", inputs=["ne var"], evaluators=(Says("16:00"),))
    bad = Case(name="bad", inputs=["ne var"], evaluators=(Says("17:00"), DoesNotSay("pi", "16")))
    report = await Dataset(name="t", cases=[good, bad]).evaluate(
        conversation(agent(model())), progress=False
    )
    assert summary(report) is False
    out = capsys.readouterr().out
    assert "ok   1/1" in out and "FAIL 0/1" in out and out.rstrip().endswith("1/2 passed")
    assert "Says: none of ('17:00',)" in out and "DoesNotSay: sends ['pi', '16']" in out
    broken = await Dataset(name="t", cases=[good]).evaluate(
        conversation(agent(FunctionModel(always_wrong))), progress=False
    )
    assert summary(broken) is False
    assert "sent lessons({'dummy': True})" in capsys.readouterr().out
