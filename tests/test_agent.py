"""The Pydantic AI adapter: a meydan tool as an agent's tool, a transcript as its history.
No model is called: Pydantic AI's test model calls every tool once."""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse
from pydantic_ai.models.test import TestModel

from meydan.agent import as_tool, history
from meydan.runtime.conversation import Said
from meydan.tools import ToolContext, ToolError

ACTOR = uuid.uuid4()
T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def said(direction: str, text: str) -> Said:
    return Said(T0, direction, None, text)


def join_list(ctx: ToolContext, day: str, guests: int = 1) -> dict:
    """Put the person on the list for a day."""
    if guests > 4:
        raise ToolError("at most four guests")
    ctx.session.append((ctx.actor_id, day, guests))
    return {"joined": day}


def run_with(**call_args):
    """An agent with the tool, run once by the test model, which calls it with `call_args`."""
    calls, closed = [], []

    @contextmanager
    def context(run_ctx):
        try:
            yield ToolContext(actor_id=run_ctx.deps, session=calls)
        finally:
            closed.append(True)

    tool = as_tool(join_list, context)
    agent = Agent(TestModel(), deps_type=uuid.UUID, tools=[tool])
    return agent, tool, calls, closed


async def test_the_person_comes_from_the_context_never_from_the_model():
    agent, tool, calls, closed = run_with()
    await agent.run("put me down for friday", deps=ACTOR)
    [(actor, _day, guests)] = calls
    assert actor == ACTOR and guests == 1  # the test model filled `day`; the default held
    assert closed == [True]  # the context was closed after the call
    assert (tool.__name__, tool.__doc__) == ("join_list", "Put the person on the list for a day.")


async def test_a_business_rule_failure_is_a_result_the_model_can_explain():
    @contextmanager
    def context(run_ctx):
        yield ToolContext(actor_id=ACTOR, session=[])

    tool = as_tool(join_list, context)
    assert tool(None, day="2026-11-13", guests=9) == {"error": "at most four guests"}
    assert tool(None, day="2026-11-13") == {"joined": "2026-11-13"}


async def test_a_tool_must_take_a_context():
    with pytest.raises(TypeError, match="takes no ToolContext"):
        as_tool(lambda: None, None)


async def test_history_merges_each_sides_lines_and_starts_with_the_person():
    lines = [
        said("out", "a reminder we sent first"),  # dropped: a history starts with the person
        said("in", "selam"),
        said("in", "yarın müsait misiniz"),
        said("out", "Merhaba!"),
        said("out", ""),  # a template with no text: skipped
        said("out", "Hangi hizmet için?"),
        said("in", "manikür"),
    ]
    request, response, last = history(lines)
    assert isinstance(request, ModelRequest) and isinstance(response, ModelResponse)
    assert request.parts[0].content == "selam\nyarın müsait misiniz"
    assert response.parts[0].content == "Merhaba!\nHangi hizmet için?"
    assert last.parts[0].content == "manikür"
    assert history([]) == []
