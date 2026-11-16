"""The Pydantic AI adapter: a meydan tool as an agent's tool, a transcript as its history.
No model is called: Pydantic AI's test model calls every tool once."""

import asyncio
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from meydan.agent import as_tool, handler, history
from meydan.channels.base import Address, Inbound, Outbound
from meydan.clock import TR, Clock
from meydan.reply import Reply
from meydan.runtime import Turn
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


def a_turn(text: str) -> Turn:
    sender = Address("tel", "+10000000001")
    message = Inbound("whatsapp", "m1", Address("wa-pnid", "1"), sender, T0, "text", text)
    return Turn(uuid.uuid4(), "whatsapp", "1", sender, uuid.uuid4(), ACTOR, None, [message])


def answers(*bubbles: str) -> FunctionModel:
    """A model that answers with these bubbles, and keeps what it was given."""

    def model(messages, info):
        answers.seen = {
            "instructions": info.instructions or "",
            "prompt": messages[-1].parts[0].content,
        }
        [output] = info.output_tools
        return ModelResponse(parts=[ToolCallPart(output.name, {"bubbles": list(bubbles)})])

    return FunctionModel(model)


async def test_a_handler_runs_the_agent_and_sends_its_bubbles_with_the_time_at_the_end():
    clock = Clock("Europe/Istanbul", TR, source=lambda: T0)
    agent = Agent(answers("Merhaba!", "Buyurun."), output_type=Reply, instructions="Be kind.")
    reply = handler(agent, clock=clock, now=lambda turn: ["Open today: yes"])
    out = await reply(a_turn("selam"))
    assert [m.text for m in out] == ["Merhaba!", "Buyurun."]
    assert answers.seen["prompt"] == (
        "selam\n\n(Now (Europe/Istanbul): 11 Kasım Çarşamba 15:00\nOpen today: yes)"
    )
    assert "15:00" not in answers.seen["instructions"]  # what changes stays out of them


async def test_a_handler_gives_up_on_a_turn_that_takes_too_long():
    async def slow(messages, info):
        await asyncio.sleep(1)

    agent = Agent(FunctionModel(slow), output_type=Reply)
    with pytest.raises(TimeoutError):
        await handler(agent, timeout_s=0.05)(a_turn("selam"))


async def test_an_app_says_what_a_reply_sends_and_what_the_tools_are_given():
    agent = Agent(answers("Tamam."), output_type=Reply, deps_type=str)
    pin = Outbound.location_message(41.0, 29.0, place="Salon")
    reply = handler(
        agent, deps=lambda turn: "deps", messages=lambda out, turn: [*out.outbound(), pin]
    )
    out = await reply(a_turn("nerede"))
    assert [m.kind for m in out] == ["text", "location"]
    assert reply.prompt(a_turn("")) == "(a message with no text: a photo, a voice note)"


async def test_an_app_says_what_the_model_reads_of_a_message_that_is_not_text():
    agent = Agent(answers("Gördüm."), output_type=Reply)
    reply = handler(agent, said=lambda turn: f"[Contact card: {turn.text}]")
    await reply(a_turn("Ece"))
    assert answers.seen["prompt"] == "[Contact card: Ece]"


async def test_the_apps_hooks_run_off_the_event_loop():
    """`now` and `messages` may read the app's database: they never run on the event loop."""
    main = threading.get_ident()
    seen = {}

    def now(turn):
        seen["now"] = threading.get_ident()
        return []

    def messages(out, turn):
        seen["messages"] = threading.get_ident()
        return out.outbound()

    agent = Agent(answers("Tamam."), output_type=Reply)
    await handler(agent, now=now, messages=messages)(a_turn("selam"))
    assert seen["now"] != main
    assert seen["messages"] != main
