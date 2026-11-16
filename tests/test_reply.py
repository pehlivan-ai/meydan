"""A reply as bubbles: its shape is checked where it is made, and an app can change every part.
No model is called: Pydantic AI's function model plays the model."""

import re

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from meydan.channels.base import Outbound
from meydan.reply import Reply, ReplyStyle


def test_bubbles_become_text_messages_in_order():
    reply = Reply(bubbles=["  Bugün üç ders var. ", "", "İlki 10:00'da."])
    assert reply.bubbles == ["Bugün üç ders var.", "İlki 10:00'da."]  # trimmed, blanks dropped
    assert reply.outbound() == [
        Outbound.text_message("Bugün üç ders var."),
        Outbound.text_message("İlki 10:00'da."),
    ]


@pytest.mark.parametrize(
    ("bubbles", "says"),
    [
        ([], "at least one bubble"),
        (["   "], "at least one bubble"),
        (["a", "b", "c", "d"], "4 bubbles; at most 3"),
        (["x" * 601], "Bubble 1 is 601 characters; at most 600"),
        (["tamam", "## Program"], "Bubble 2 has a '#' heading"),
        (["Bu **çok** önemli"], "'**bold**'"),
        (["| gün | saat |"], "a table row"),
        (["Adres [burada](https://example.com)"], "a '[text](url)' link"),
        (["Dersler şöyle:", "10:00 bale"], "Bubble 1 ends with ':'"),
    ],
)
def test_a_reply_out_of_shape_is_refused_with_a_sentence(bubbles, says):
    with pytest.raises(ValidationError, match=re.escape(says)):
        Reply(bubbles=bubbles)


def test_what_messaging_apps_format_themselves_is_left_alone():
    Reply(bubbles=["*Salı* 10:00\n- bale\n- piyano", "Saat 10:00'da görüşürüz, 3 #etiket"])


def test_an_app_sets_its_own_style_and_fields():
    class Short(Reply):
        style = ReplyStyle(max_bubbles=1, max_chars=20, markdown=True, dangling=True)
        pin: str | None = None

    Short(bubbles=["**tamam** şöyle:"], pin="kulup")
    with pytest.raises(ValidationError, match="2 bubbles; at most 1"):
        Short(bubbles=["bir", "iki"])
    with pytest.raises(ValidationError, match="at most 20"):
        Short(bubbles=["x" * 21])
    Reply(bubbles=["bir", "iki"])  # the default is untouched

    class Free(Reply):
        style = ReplyStyle(max_bubbles=10, max_chars=None)

    Free(bubbles=["x" * 5000])


def test_the_schema_the_model_is_given_carries_the_bubble_count():
    class Two(Reply):
        style = ReplyStyle(max_bubbles=2)

    assert Reply.model_json_schema()["properties"]["bubbles"]["maxItems"] == 3
    two = Two.model_json_schema()["properties"]["bubbles"]
    assert (two["minItems"], two["maxItems"]) == (1, 2)


def test_problems_work_on_plain_strings():
    style = ReplyStyle(max_chars=5)
    assert style.problems(["tamam"]) == []
    [problem] = style.problems(["çok uzun"])
    assert problem.startswith("Bubble 1 is 8 characters; at most 5")


def test_a_style_with_impossible_numbers_is_refused():
    with pytest.raises(ValueError, match="max_bubbles"):
        ReplyStyle(max_bubbles=0)
    with pytest.raises(ValueError, match="max_chars"):
        ReplyStyle(max_chars=0)


async def test_an_agent_is_told_what_was_wrong_and_writes_the_reply_again():
    seen: list[str] = []
    schemas: list[dict] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        [output] = info.output_tools
        schemas.append(output.parameters_json_schema)
        if not seen:  # first try: a heading and a bubble that leans on the next
            seen.append("")
            return ModelResponse(
                parts=[ToolCallPart(output.name, {"bubbles": ["# Program", "Dersler:"]})]
            )
        seen.append(str(messages[-1].parts))  # what came back for the refused reply
        return ModelResponse(
            parts=[ToolCallPart(output.name, {"bubbles": ["Bugün üç ders var.", "İlki 10:00."]})]
        )

    agent = Agent(FunctionModel(model), output_type=Reply)
    result = await agent.run("bugün ne var")
    assert result.output.bubbles == ["Bugün üç ders var.", "İlki 10:00."]
    _, told = seen
    assert "'#' heading" in told and "ends with ':'" in told
    assert schemas[0]["properties"]["bubbles"]["maxItems"] == 3
