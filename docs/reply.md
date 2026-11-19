# Reply: an answer as bubbles

Available in `meydan.reply`. Needs only Pydantic: no agent framework, no runtime.

People on WhatsApp or Telegram write short messages, one after another. An agent that answers
with one long block reads like a document. For an agent that talks to a person on a messaging
app, we recommend that the reply is **structured output, a list of bubbles**, and that the model
decides where one thought ends and the next begins.

```python
from meydan.reply import Reply

agent = Agent(model, output_type=Reply, tools=[...])  # Pydantic AI


async def reply(turn: Turn) -> list[Outbound]:
    result = await agent.run(turn.text, ...)
    return result.output.outbound()  # one text message per bubble
```

The model returns `{"bubbles": ["Bugün üç ders var.", "İlki 10:00'da."]}`. The runtime sends
them in order, paced like typing ([docs/presence.md](presence.md)).

## Why not split the text afterwards

Cutting a finished text at line breaks or sentences cuts thoughts in the middle. Where a bubble
ends is a decision about meaning, so it belongs to whoever wrote the text.

## The shape is checked where the reply is made

`ReplyStyle` is the shape; a reply that breaks it is refused with a sentence the model can act
on. An agent framework that validates its output sends that sentence back, and the model writes
the reply again. The prompt then only carries the tone ("match the person's length").

| Check | Default | Setting |
|---|---|---|
| How many bubbles | 1 to 3; also written into the JSON schema the model is given | `max_bubbles` |
| Characters in one bubble | 600. A starting point, not a measurement: set it from what your people read | `max_chars` (`None`: no limit of ours) |
| Markdown a messaging app shows as typed: `#` headings, `**bold**`, table rows, `[text](url)` links | refused | `markdown=True` allows it |
| A bubble that ends with `:` and leans on the next one | refused | `dangling=True` allows it |

Blank bubbles are dropped and the rest trimmed. What WhatsApp formats itself (`*bold*`,
`_italic_`, lists) is left alone.

With Pydantic AI, a refused reply uses the agent's output retries (one by default,
`Agent(..., retries=3)` for more). When they run out the turn fails, and nothing half-shaped is
sent.

## Your own numbers, your own fields

Subclass `Reply`:

```python
from meydan.reply import Reply, ReplyStyle


class DeskReply(Reply):
    style = ReplyStyle(max_bubbles=2, max_chars=300)
    # Anything that is not text is its own field and names a thing; code looks it up. A made-up
    # name then sends nothing, instead of a made-up address.
    pin: str | None = None


async def reply(turn: Turn) -> list[Outbound]:
    out = (await agent.run(turn.text, ...)).output
    messages = out.outbound()
    if out.pin and (place := places.get(out.pin)):
        messages.append(Outbound.location_message(place.lat, place.lng, place=place.name))
    return messages
```

To describe `bubbles` to the model in your own words, declare the field again in the subclass
with your own `Field(description=...)`.

## Choices

A question with options to tap, instead of an answer typed in words:

```python
from meydan.channels import Choice

Outbound.choices_message(
    "What's your level?",
    [Choice("lvl:1", "Beginner", "Just started"), Choice("lvl:2", "Intermediate"), ...],
    label="Pick one",  # the button that opens a list
)
```

- 1-10 options; ids and titles must differ. The picked option's `id` comes back:
  `meydan.channels.whatsapp.reply_payload(message)` (a button or a list row alike), and the
  message's `text` is its title, so a model reading the conversation sees what was tapped.
- The channel decides how it looks. WhatsApp: up to 3 options with titles up to 20 characters and
  no descriptions are reply buttons (one tap); anything else is a list (up to 10 rows, titles up
  to 24, descriptions up to 72, opened by `label`, up to 20). `whatsapp.choice_style(message)`
  says which, or raises `ValueError` when neither fits, so you can check before queueing. Telegram:
  one inline button per row.
- `text` keeps the question and the options (`"question\n• A\n• B"`), so transcripts show what
  was offered.
- Choices are free-form (no template): WhatsApp sends them only inside the 24 h window.

As with a pin, let the model name the options as its own field and build the message in code; for
a form, take the options from your data, not from the model.

## None of this is required

The defaults are a recommendation, not a rule. A turn's handler returns a list of `Outbound`,
however it was made:

- change any number or switch a check off (`ReplyStyle`);
- keep your own output type and use only the checks: `ReplyStyle().problems(["..."])` takes plain
  strings and returns what is wrong, as sentences;
- or leave the module out and return one `Outbound.text_message(...)`.

## What it does not check

Whether what the reply says is true. A check that a reply claims nothing the tools did not do
depends on the app's own operations, so it stays in the app for now.
