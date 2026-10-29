"""Observability: one record per turn and per model call, written with the reply."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from conftest import T0, FakeChannel, msg, receive, runtime
from sqlalchemy import select

from meydan.channels.base import Outbound
from meydan.observe import Usage, current_turn, record_run, record_usage, usage_from_messages


@dataclass
class FakeUsage:  # shaped like Pydantic AI's RequestUsage
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost: Decimal | None = None


@dataclass
class FakeResponse:  # shaped like Pydantic AI's ModelResponse
    model_name: str
    usage: FakeUsage
    provider_name: str = "openrouter"
    provider_details: dict[str, Any] = field(default_factory=dict)
    kind: str = "response"


@dataclass
class FakeRequest:
    kind: str = "request"


class FakeRun:
    def __init__(self, *messages):
        self.messages = list(messages)

    def new_messages(self):
        return self.messages


def test_cost_prefers_the_provider_then_the_estimate():
    usage = usage_from_messages(
        [
            FakeRequest(),
            FakeResponse(
                "m1", FakeUsage(10, 5, cost=Decimal("0.9")), provider_details={"cost": 0.002}
            ),
            FakeResponse("m2", FakeUsage(20, 8, cost=Decimal("0.001"))),
            FakeResponse("m3", FakeUsage(1, 1)),
        ]
    )
    assert [(u.model, u.cost_usd, u.cost_source) for u in usage] == [
        ("m1", Decimal("0.002"), "provider"),
        ("m2", Decimal("0.001"), "estimate"),
        ("m3", None, None),
    ]


def test_recording_outside_a_turn_does_nothing():
    assert current_turn() is None
    record_usage(Usage("p", "m", input_tokens=1, output_tokens=1))  # no error, nothing to attach to


async def test_turn_record_carries_timing_tokens_and_cost(db):
    async def agent(turn):
        assert current_turn() is not None and current_turn().conversation_id == turn.conversation_id
        record_run(
            FakeRun(
                FakeResponse("deepseek", FakeUsage(100, 20), provider_details={"cost": 0.0003}),
                FakeResponse("deepseek", FakeUsage(150, 30), provider_details={"cost": 0.0004}),
            )
        )
        return [Outbound.text_message("bir"), Outbound.text_message("iki")]

    rt = runtime(db, agent, FakeChannel())
    receive(rt, [msg("w1", "selam", T0), msg("w2", "turnuva?", T0)])
    await rt.work_once()
    with rt.engine.connect() as c:
        turn = c.execute(select(rt.t.turns)).one()
        calls = c.execute(select(rt.t.usage)).all()
        out = c.execute(select(rt.t.outbox.c.turn_id)).scalars().all()
    assert turn.status == "replied" and turn.messages_in == 2 and turn.messages_out == 2
    assert turn.model_calls == 2 and turn.input_tokens == 250 and turn.output_tokens == 50
    assert turn.cost_usd == Decimal("0.0007") and turn.cost_source == "provider"
    assert turn.finished_at >= turn.started_at and turn.actor_id is not None
    assert len(calls) == 2 and set(out) == {turn.id}  # the reply carries the turn's id


async def test_failed_turns_are_recorded_with_their_cost(db):
    async def broken(turn):
        cost = Decimal("0.0001")
        used = Usage("openrouter", "m", input_tokens=50, cost_usd=cost, cost_source="provider")
        record_usage(used)
        raise RuntimeError("model down")

    rt = runtime(db, broken, FakeChannel(), max_turn_attempts=2)
    rt.on_failure = lambda turn, exc: [Outbound.text_message("Bir sorun oldu.")]
    receive(rt, [msg("w1", "selam", T0)])
    await rt.work_once()
    await rt.work_once()
    with rt.engine.connect() as c:
        turns = c.execute(select(rt.t.turns).order_by(rt.t.turns.c.started_at)).all()
    assert [t.status for t in turns] == ["failed", "gave_up"]
    assert all("model down" in t.error for t in turns)
    assert sum(t.cost_usd for t in turns) == Decimal("0.0002")
    assert turns[1].messages_out == 1  # the failure reply
