"""A reply as bubbles: what an agent that talks to a person on a messaging app returns.

People on WhatsApp or Telegram write short messages, one after another. An agent that answers
with one long block reads like a document. So the reply is structured output, a list of
bubbles, and the model decides where one thought ends and the next begins:

    agent = Agent(model, output_type=Reply, tools=[...])
    result = await agent.run(turn.text, ...)
    return result.output.outbound()  # one Outbound per bubble; the runtime paces them

The shape is checked where the reply is made, not in the prompt: `ReplyStyle` says how many
bubbles, how long, and what a messaging app would show as clutter. A reply that breaks it is
refused with a sentence the model can act on; an agent framework that validates its output
(Pydantic AI does) sends that sentence back and the model writes the reply again.

    class Reply(meydan.reply.Reply):
        style = ReplyStyle(max_bubbles=2, max_chars=300)  # your own numbers
        pin: str | None = None  # your own fields: name a thing, let code look it up

The defaults are a recommendation for agents that talk to people on WhatsApp and Telegram, not
a rule: an app may change every number, switch a check off, or return its own type and never
import this module. `ReplyStyle.problems` works on plain strings, for an app that has its own.

Needs only Pydantic: no agent framework, no runtime.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from meydan.channels.base import Outbound

# What messaging apps show as typed, not as formatting: headings, double-asterisk bold,
# table rows and [text](url) links. WhatsApp's own marks (*bold*, _italic_, lists) are not here.
_MARKDOWN = (
    (re.compile(r"(?m)^\s{0,3}#{1,6}\s"), "a '#' heading"),
    (re.compile(r"\*\*[^*\n]+\*\*"), "'**bold**'"),
    (re.compile(r"(?m)^\s*\|.*\|\s*$"), "a table row"),
    (re.compile(r"\[[^\]\n]+\]\([^)\n]+\)"), "a '[text](url)' link"),
)


@dataclass(frozen=True)
class ReplyStyle:
    """The shape of a reply. The defaults suit a person-facing agent on a messaging app."""

    max_bubbles: int = 3  # messages sent one after another for one reply
    # Characters in one bubble; None for no limit of ours. A starting point, not a measurement:
    # set it from what your people read. Channels refuse above their own limit (`text_max` in
    # meydan's channel rules, `meydan/rules.py`).
    max_chars: int | None = 600
    markdown: bool = False  # allow headings, '**bold**', tables and '[text](url)' links
    dangling: bool = False  # allow a bubble that ends with ':' and leans on the next one

    def __post_init__(self) -> None:
        if self.max_bubbles < 1:
            raise ValueError("max_bubbles must be at least 1")
        if self.max_chars is not None and self.max_chars < 1:
            raise ValueError("max_chars must be at least 1, or None")

    def problems(self, bubbles: Sequence[str]) -> list[str]:
        """What is wrong with these bubbles, as sentences a model can act on. Empty when fine."""
        found = []
        if not bubbles:
            found.append("A reply needs at least one bubble with text.")
        if len(bubbles) > self.max_bubbles:
            found.append(
                f"{len(bubbles)} bubbles; at most {self.max_bubbles}. Say less, or join "
                "thoughts that belong together."
            )
        for n, text in enumerate(bubbles, start=1):
            if self.max_chars is not None and len(text) > self.max_chars:
                found.append(
                    f"Bubble {n} is {len(text)} characters; at most {self.max_chars}. Say it "
                    "shorter, or split it where one thought ends."
                )
            if not self.markdown:
                for pattern, name in _MARKDOWN:
                    if pattern.search(text):
                        found.append(
                            f"Bubble {n} has {name}, which a messaging app shows as typed. "
                            "Write plain text."
                        )
            if not self.dangling and text.endswith(":"):
                found.append(
                    f"Bubble {n} ends with ':' and needs the next one to make sense. Each "
                    "bubble is one complete thought."
                )
        return found


def _schema(schema: dict[str, Any], model: type["Reply"]) -> None:
    """Put the style's bubble count into the JSON schema the model is given."""
    bubbles = schema.get("properties", {}).get("bubbles")
    if bubbles is not None:
        bubbles["minItems"] = 1
        bubbles["maxItems"] = model.style.max_bubbles


class Reply(BaseModel):
    """An agent's reply to a person: one or more bubbles. Subclass it to set `style`, to add
    fields, or to describe `bubbles` in your own words."""

    style: ClassVar[ReplyStyle] = ReplyStyle()
    model_config = ConfigDict(json_schema_extra=_schema)

    bubbles: list[str] = Field(
        description=(
            "Messages sent one after another, as a person would text them. Each is one "
            "complete thought that makes sense alone. Usually one or two."
        )
    )

    @field_validator("bubbles")
    @classmethod
    def _trimmed(cls, bubbles: list[str]) -> list[str]:
        return [b.strip() for b in bubbles if b.strip()]

    @model_validator(mode="after")
    def _shaped(self) -> "Reply":
        if found := type(self).style.problems(self.bubbles):
            raise ValueError(" ".join(found))
        return self

    def outbound(self) -> list[Outbound]:
        """The bubbles as text messages, in order: what a turn's handler returns. Add your own
        messages (a map pin, a template) after them."""
        return [Outbound.text_message(b) for b in self.bubbles]
