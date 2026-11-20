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

## Your own numbers

Subclass `Reply` and set `style`:

```python
from meydan.reply import Reply, ReplyStyle


class DeskReply(Reply):
    style = ReplyStyle(max_bubbles=2, max_chars=300, max_attachments=2)
```

To describe `bubbles` to the model in your own words, declare the field again in the subclass
with your own `Field(description=...)`.

## Attachments: what goes after the bubbles

A map pin, a photo, a contact card, choices to tap. The model doesn't write the address, the
image or the number: it **names a thing** (a venue's slug, an event's id, a purpose) and your
code finds the real value. A made-up name then sends nothing, instead of a made-up address.

Pick a primitive, name the thing, and say how to find it:

```python
from typing import Literal

from pydantic import Field

from meydan.reply import Attachments, Choices, Location, Place, Reply


class Venue(Location):
    type: Literal["location"] = "location"
    when = "When they ask where it is or how to get there."
    venue: str = Field(description="The venue's slug, from a tool result.")

    def resolve(self, ctx) -> Place | None:
        v = venues.get(ctx.session, self.venue)
        return Place(v.lat, v.lng, v.name, v.address, url=v.maps_url) if v else None


class DeskReply(Reply):
    attachments: Attachments[Venue, Choices] = Field(
        default_factory=list, description="Sent after the bubbles, in this order."
    )


messages = result.output.outbound(ctx)  # bubbles, then each attachment's messages
```

| Primitive | `resolve` returns | Sends |
| --- | --- | --- |
| `Location` | `Place(lat, lng, name, address, url)` | a map pin, and `url` right after it if set |
| `Image` | `ImageRef(media, alt, caption)` (a key from `meydan.media.put`) | a photo |
| `Contact` | `Person(name, phone, title, org)` | a contact card |
| `Choices` | nothing to resolve: the model writes the question and the options | buttons or a list |

- Each type has a fixed `type` (a `Literal`): the schema tells them apart by it.
- **What the model reads decides when it reaches for one.** `when` becomes the type's
  description in the schema (the docstring stays yours); every field the model fills gets a
  `description`. Not enforced: put `assert_described(DeskReply)` in your tests and it lists
  whatever the model would read without a word.
- A reply may be attachments alone, or bubbles alone, never nothing; at most
  `style.max_attachments` (3).
- `ctx` is yours (a database session, who wrote): `outbound(ctx)` hands it to each attachment.
- One that resolves to `None`, or raises, is left out and logged (its type only); the rest go.
- `Choices` needs no code; subclass it to set `label` (the button that opens a list) in your
  language, or to fix the question and options. Option titles are at most 24 characters in the
  schema, so the model is told before WhatsApp would refuse.
- Something no primitive fits: subclass `Attachment` and write `to_outbound(ctx)` yourself.
- A reply class without `attachments` of its own shows the model no attachments at all.

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

## A contact card

`Outbound.contact_message(name, phone, org=, title=)`: a person someone can message, call or save
in one tap (`phone` E.164 with "+"). On WhatsApp the card carries the number's WhatsApp id, so it
shows "Message" and "Save contact" (without one WhatsApp would offer only "Invite"); on Telegram it
is a contact. `text` keeps the name, title, organisation and number for transcripts. As with a
pin, let the model ask for the card by a field and fill the number in from your own settings.

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
