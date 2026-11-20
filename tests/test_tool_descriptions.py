"""A tool as the model reads it: its docstring, its parameters and what it returns, each with a word
on it. A field of the result with a description is sent to the model; one without is a guess."""

import contextlib

import pytest
from pydantic import BaseModel, Field

from meydan.agent import assert_tool_described, tool, undescribed_tool
from meydan.tools import ToolContext


class Slot(BaseModel):
    time: str = Field(description="When it starts, HH:MM.")
    places_left: int = Field(description="Places still free. 0: full, offer the next time.")


class Bare(BaseModel):
    time: str
    places_left: int


class Day(BaseModel):
    date: str = Field(description="ISO date.")
    slots: list[Slot] = Field(description="Its times, earliest first.")


def described(ctx: ToolContext, subject: str) -> list[Slot]:
    """Free trial times.

    Args:
        subject: The subject, as listed.
    """
    return []


def bare_return(ctx: ToolContext, subject: str) -> list[Bare]:
    """Free trial times.

    Args:
        subject: The subject, as listed.
    """
    return []


def nested(ctx: ToolContext) -> Day:
    """A day."""
    return Day(date="", slots=[])


def plain(ctx: ToolContext, n: int) -> dict:
    return {}


@contextlib.contextmanager
def none(ctx):
    yield None


def test_a_described_return_is_sent_to_the_model_and_a_bare_one_is_not():
    assert tool(described, none).include_return_schema is True
    assert not tool(bare_return, none).include_return_schema  # nothing said: nothing extra sent
    assert not tool(plain, none).include_return_schema
    assert tool(nested, none).include_return_schema is True


def test_undescribed_names_the_tool_its_parameters_and_the_fields_it_returns():
    assert undescribed_tool(described) == []
    assert undescribed_tool(nested) == []
    found = undescribed_tool(bare_return)
    assert [f.split(" with ")[0] for f in found] == [
        "bare_return returns [].time",
        "bare_return returns [].places_left",
    ]
    found = undescribed_tool(plain)
    assert found[0].startswith("plain has no docstring")
    assert found[1].startswith("plain(n) has no description")


def test_assert_tool_described_fails_listing_everything():
    assert_tool_described(described, nested)
    with pytest.raises(AssertionError, match=r"(?s)returns \[\]\.time.*plain has no docstring"):
        assert_tool_described(bare_return, plain)


def test_a_tool_still_runs_through_its_context_and_errors_reach_the_model():
    from meydan.tools import ToolError

    def refuses(ctx: ToolContext) -> Slot:
        """Refuses."""
        raise ToolError("This time is full. The next free time is Sunday 11:00.")

    t = tool(refuses, none)
    assert t.function(object()) == {
        "error": "This time is full. The next free time is Sunday 11:00."
    }
