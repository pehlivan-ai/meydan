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

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from meydan.channels.base import Choice, Outbound

log = logging.getLogger("meydan.reply")

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
    # UTF-8 bytes in one bubble, for a channel that counts bytes: set it from the channel's
    # rules, e.g. `DEFAULT.of("instagram").text_bytes_max`. None: no byte limit.
    max_bytes: int | None = None
    markdown: bool = False  # allow headings, '**bold**', tables and '[text](url)' links
    dangling: bool = False  # allow a bubble that ends with ':' and leans on the next one
    max_attachments: int = 3  # things sent after the bubbles (a pin, a photo, a card, choices)

    def __post_init__(self) -> None:
        if self.max_bubbles < 1:
            raise ValueError("max_bubbles must be at least 1")
        if self.max_attachments < 0:
            raise ValueError("max_attachments must be 0 or more")
        if self.max_chars is not None and self.max_chars < 1:
            raise ValueError("max_chars must be at least 1, or None")
        if self.max_bytes is not None and self.max_bytes < 1:
            raise ValueError("max_bytes must be at least 1, or None")

    def problems(self, bubbles: Sequence[str], attachments: int | None = None) -> list[str]:
        """What is wrong with these bubbles, as sentences a model can act on. Empty when fine.
        `attachments`: how many go with them, when the reply can carry any (then a reply may be
        attachments alone); None when it can't."""
        found = []
        if not bubbles and not attachments:
            found.append(
                "A reply needs at least one bubble with text."
                if attachments is None
                else "A reply needs at least one bubble with text, or an attachment."
            )
        if attachments is not None and attachments > self.max_attachments:
            found.append(
                f"{attachments} attachments; at most {self.max_attachments}. Keep the ones that "
                "answer what they asked."
            )
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
            elif self.max_bytes is not None and len(text.encode()) > self.max_bytes:
                found.append(
                    f"Bubble {n} is too long for this channel ({len(text.encode())} bytes; at "
                    f"most {self.max_bytes}). Say it shorter, or split it where one thought ends."
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


# --- Attachments ------------------------------------------------------------------------------
# What a reply sends after its bubbles: a map pin, a photo, a contact card, choices to tap. The
# model names a thing (a venue's slug, a tournament's id, a purpose); code finds the real value
# (`resolve`), so a made-up name sends nothing instead of a made-up address or number. meydan
# turns what `resolve` returns into the channel's message and keeps the channel's limits.
#
# What the model reads decides when it reaches for an attachment: give each type `when` (when to
# add it; it becomes the attachment's description in the schema) and a `description` on every
# field the model fills. Not enforced; `assert_described(YourReply)` in your tests says what is
# missing.


class Attachment(BaseModel):
    """Something sent after the bubbles. Subclass a primitive below (Location, Image, Contact,
    Choices) and give it `type: Literal["..."]`, `when` and described fields; or subclass this
    directly and write `to_outbound`."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=lambda s, m: _described(s, m))

    type: str
    when: ClassVar[str] = ""  # when the model should add it (see assert_described)

    def to_outbound(self, ctx: Any) -> list[Outbound]:
        """The messages this attachment sends; [] sends nothing (logged)."""
        raise NotImplementedError(f"{type(self).__name__} must implement to_outbound")


def _concrete(cls: type[Attachment]) -> bool:
    """A type the model can pick: its `type` is a fixed name (Literal)."""
    ann = cls.model_fields["type"].annotation
    return get_origin(ann) is Literal


def _described(schema: dict[str, Any], model: type[BaseModel]) -> None:
    """An attachment's description in the schema is its `when`, not its docstring (that one is
    for the developer)."""
    when = getattr(model, "when", "").strip()
    if when:
        schema["description"] = when
    else:
        schema.pop("description", None)


@dataclass(frozen=True)
class Place:
    """Where a Location points: WGS 84, the name and address shown under the pin, and a link
    (e.g. a maps link) sent right after it."""

    lat: float
    lng: float
    name: str | None = None
    address: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class ImageRef:
    """An image kept with `meydan.media.put`; `alt` says in transcripts what it was."""

    media: str
    alt: str | None = None
    caption: str | None = None


@dataclass(frozen=True)
class Person:
    """Whose contact card to send; phone E.164 with "+"."""

    name: str
    phone: str
    title: str | None = None
    org: str | None = None


class Location(Attachment):
    """A map pin, and its link right after it when `Place.url` is set. Implement `resolve`, or
    declare it from a table with `Location.of(...)`."""

    @classmethod
    def of(
        cls, table: Any, *, by: Any, type: str, lat: Any, lng: Any, name: Any = None,
        address: Any = None, url: Any = None, field: str | None = None, when: str = "",
        describe: str = "", where: dict[Any, Any] | None = None, visible: Any = None,
    ) -> type["Location"]:  # fmt: skip
        """A Location found by `by` in `table`. Columns or templates ("{name}") for each part;
        a row without coordinates resolves to nothing."""

        def build(row: Any) -> Place | None:
            la, ln = _value(row, lat), _value(row, lng)
            if la is None or ln is None:
                return None
            return Place(la, ln, _value(row, name), _value(row, address), _value(row, url))

        cols = {"lat": lat, "lng": lng, "name": name, "address": address, "url": url}
        return _declared(cls, table, by=by, type=type, field=field, when=when,
                         describe=describe, columns=cols, where=where, visible=visible,
                         build=build)  # type: ignore[return-value]  # fmt: skip

    def resolve(self, ctx: Any) -> Place | None:
        raise NotImplementedError(f"{type(self).__name__} must implement resolve")

    def to_outbound(self, ctx: Any) -> list[Outbound]:
        place = self.resolve(ctx)
        if place is None:
            return []
        out = [
            Outbound.location_message(place.lat, place.lng, place=place.name, address=place.address)
        ]
        return out + ([Outbound.text_message(place.url)] if place.url else [])


class Image(Attachment):
    """A photo, as a person sends one. Implement `resolve`, or declare it from a table with
    `Image.of(...)`."""

    @classmethod
    def of(
        cls, table: Any, *, by: Any, type: str, media: Any, alt: Any = None,
        caption: Any = None, field: str | None = None, when: str = "", describe: str = "",
        where: dict[Any, Any] | None = None, visible: Any = None,
    ) -> type["Image"]:  # fmt: skip
        """An Image found by `by` in `table`; `media` is the column holding the meydan.media
        key (a row without one resolves to nothing)."""

        def build(row: Any) -> ImageRef | None:
            key = _value(row, media)
            if not key:
                return None
            return ImageRef(key, alt=_value(row, alt), caption=_value(row, caption))

        cols = {"media": media, "alt": alt, "caption": caption}
        return _declared(cls, table, by=by, type=type, field=field, when=when,
                         describe=describe, columns=cols, where=where, visible=visible,
                         build=build)  # type: ignore[return-value]  # fmt: skip

    def resolve(self, ctx: Any) -> ImageRef | None:
        raise NotImplementedError(f"{type(self).__name__} must implement resolve")

    def to_outbound(self, ctx: Any) -> list[Outbound]:
        ref = self.resolve(ctx)
        if ref is None:
            return []
        return [Outbound.image_message(ref.media, caption=ref.caption, alt=ref.alt)]


class Contact(Attachment):
    """A contact card someone can message, call or save. Implement `resolve`, or declare it
    from a table with `Contact.of(...)`."""

    @classmethod
    def of(
        cls, table: Any, *, by: Any, type: str, name: Any, phone: Any, title: Any = None,
        org: Any = None, field: str | None = None, when: str = "", describe: str = "",
        where: dict[Any, Any] | None = None, visible: Any = None,
    ) -> type["Contact"]:  # fmt: skip
        """A Contact found by `by` in `table` (a row without a name or phone resolves to
        nothing)."""

        def build(row: Any) -> Person | None:
            n, p = _value(row, name), _value(row, phone)
            if not n or not p:
                return None
            return Person(n, p, title=_value(row, title), org=_value(row, org))

        cols = {"name": name, "phone": phone, "title": title, "org": org}
        return _declared(cls, table, by=by, type=type, field=field, when=when,
                         describe=describe, columns=cols, where=where, visible=visible,
                         build=build)  # type: ignore[return-value]  # fmt: skip

    def resolve(self, ctx: Any) -> Person | None:
        raise NotImplementedError(f"{type(self).__name__} must implement resolve")

    def to_outbound(self, ctx: Any) -> list[Outbound]:
        p = self.resolve(ctx)
        if p is None:
            return []
        return [Outbound.contact_message(p.name, p.phone, org=p.org, title=p.title)]


# --- Declared from a table --------------------------------------------------------------------
# The common case, "find the row with this key, use these columns", without writing `resolve`:
#
#     Venue = Location.of(Club, by=Club.slug, field="venue", type="location",
#                         when="When they ask where it is.", describe="The venue's slug.",
#                         lat=Club.lat, lng=Club.lng, name=Club.name, address=Club.address,
#                         url=Club.maps_url)
#
# It makes an ordinary attachment class (its docstring says what maps to what) whose `resolve`
# runs one SELECT through the session in `ctx` (a SQLAlchemy Session, or anything with
# `.session`). A missing row, an empty required column, or `visible(ctx, row)` False resolves to
# nothing. Columns that aren't the table's fail when the class is made. Needs SQLAlchemy.


def _session(ctx: Any) -> Any:
    from sqlalchemy.orm import Session

    session = ctx if isinstance(ctx, Session) else getattr(ctx, "session", None)
    if not isinstance(session, Session):
        raise TypeError(
            "an attachment declared with .of() reads through a SQLAlchemy Session: pass one as "
            "ctx to reply.outbound(ctx), or an object with a .session attribute"
        )
    return session


def _declared(
    base: type[Attachment],
    table: Any,
    *,
    by: Any,
    type: str,
    field: str | None,
    when: str,
    describe: str,
    columns: dict[str, Any],
    where: dict[Any, Any] | None,
    visible: Any,
    build: Any,
) -> type[Attachment]:
    from pydantic import create_model
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import select

    mapper = sa_inspect(table, raiseerr=False)
    if mapper is None or not hasattr(mapper, "attrs"):
        raise TypeError(f"{table!r} is not a mapped SQLAlchemy class")
    own = {a.key for a in mapper.attrs}

    def check(name: str, col: Any) -> None:
        if col is None:
            return
        key = getattr(col, "key", None)
        if getattr(col, "class_", None) is not table or key not in own:
            raise TypeError(
                f"{base.__name__}.of({table.__name__}): {name}={col!r} is not a column of "
                f"{table.__name__}; pass {table.__name__}.<column>"
            )

    check("by", by)
    for name, col in columns.items():
        if hasattr(col, "class_"):  # a column; a template ("{name}") or a callable is the app's
            check(name, col)
    for col in where or {}:
        check("where", col)

    field = field or by.key
    kind = Literal[type]  # type: ignore[valid-type]
    cls = create_model(  # type: ignore[call-overload]
        f"{base.__name__}{table.__name__}",
        __base__=base,
        type=(kind, type),
        **{field: (str, Field(description=describe or None))},
    )
    cls.when = when
    filters = list((where or {}).items())

    def resolve(self: Any, ctx: Any) -> Any:
        key: Any = getattr(self, field)
        python_type = getattr(by.type, "python_type", str)
        if python_type is not str:
            try:
                key = python_type(key)
            except (TypeError, ValueError):
                return None
        query = select(table).where(by == key, *(c == v for c, v in filters))
        row = _session(ctx).scalar(query)
        if row is None or (visible is not None and not visible(ctx, row)):
            return None
        return build(row)

    cls.resolve = resolve
    mapping = ", ".join(
        f"{k}={getattr(v, 'key', v)!s}" for k, v in columns.items() if v is not None
    )
    cls.__doc__ = (
        f"{base.__name__} of a {table.__name__} row, found by {by.key} (the model writes "
        f"`{field}`): {mapping}."
    )
    return cls


def _value(row: Any, col: Any) -> Any:
    if col is None:
        return None
    if isinstance(col, str):  # a template over the row's columns, e.g. "{name} poster"
        from sqlalchemy import inspect as sa_inspect

        attrs = sa_inspect(row).mapper.column_attrs
        return col.format_map({a.key: getattr(row, a.key) for a in attrs})
    if callable(col) and not hasattr(col, "class_"):
        return col(row)
    return getattr(row, col.key)


class Option(BaseModel):
    """One option of Choices."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=24, description="What they tap (24 characters at most).")
    description: str | None = Field(
        default=None, max_length=72, description="An optional short line under it."
    )


class Choices(Attachment):
    """A question with options to tap, written by the model: nothing to look up. Buttons for up
    to 3 short options, a list otherwise (the channel decides). Subclass it to set `label` (the
    button that opens a list) in your language, or to fix the question and options in code."""

    type: Literal["choices"] = "choices"
    when: ClassVar[str] = (
        "When the answer is one of a few known options (a yes/no confirmation, a choice from a "
        "form): they tap instead of typing. A tap comes back as the option's title."
    )
    label: ClassVar[str] = "Options"

    question: str = Field(description="The question, sent with the options under it.")
    options: list[Option] = Field(
        min_length=1, max_length=10, description="1 to 10 options, titles all different."
    )

    def to_outbound(self, ctx: Any) -> list[Outbound]:
        try:
            choices = [
                Choice(f"choice:{i}", o.title, o.description) for i, o in enumerate(self.options)
            ]
            return [Outbound.choices_message(self.question, choices, label=type(self).label)]
        except ValueError as e:  # e.g. the same title twice: the question still goes, as text
            log.warning("choices sent as text: %s", e)
            lines = [self.question, *(f"• {o.title}" for o in self.options)]
            return [Outbound.text_message("\n".join(lines))]


class _Attachments:
    """`Attachments[Venue, Poster, Choices]`: the attachment types a reply may carry, told apart
    by their `type`. Use it as the type of `attachments` in your Reply subclass."""

    def __class_getitem__(cls, types: Any) -> Any:
        types = types if isinstance(types, tuple) else (types,)
        for t in types:
            if not (isinstance(t, type) and issubclass(t, Attachment) and _concrete(t)):
                raise TypeError(
                    f"{t!r} is not an attachment type the model can pick: subclass a primitive "
                    'and give it `type: Literal["..."]`.'
                )
        if len(types) == 1:
            return list[types[0]]
        return list[Annotated[Union[types], Field(discriminator="type")]]  # noqa: UP007


Attachments = _Attachments


def _schema(schema: dict[str, Any], model: type["Reply"]) -> None:
    """Put the style's bubble count into the JSON schema the model is given."""
    props = schema.get("properties", {})
    attachments = props.get("attachments")
    bubbles = props.get("bubbles")
    if bubbles is not None:
        # With attachments a reply may be attachments alone; the validator checks the pair.
        bubbles["minItems"] = 0 if attachments is not None else 1
        bubbles["maxItems"] = model.style.max_bubbles
    if attachments is not None:
        attachments["maxItems"] = model.style.max_attachments


class Reply(BaseModel):
    """An agent's reply to a person: one or more bubbles. Subclass it to set `style`, to add
    fields, or to describe `bubbles` in your own words."""

    style: ClassVar[ReplyStyle] = ReplyStyle()
    model_config = ConfigDict(json_schema_extra=_schema)

    bubbles: list[str] = Field(
        default_factory=list,
        description=(
            "Messages sent one after another, as a person would text them. Each is one "
            "complete thought that makes sense alone. Usually one or two."
        ),
    )

    # Hidden from the model unless a subclass says which types it may use:
    # `attachments: Attachments[Venue, Poster] = Field(default_factory=list, description=...)`.
    attachments: SkipJsonSchema[list[Attachment]] = Field(default_factory=list)

    # True once a subclass says which attachment types it carries.
    _attaches: ClassVar[bool] = False

    @classmethod
    def __pydantic_init_subclass__(cls, **kw: Any) -> None:
        super().__pydantic_init_subclass__(**kw)
        base = Reply.model_fields["attachments"].annotation
        cls._attaches = cls.model_fields["attachments"].annotation != base

    @field_validator("bubbles")
    @classmethod
    def _trimmed(cls, bubbles: list[str]) -> list[str]:
        return [b.strip() for b in bubbles if b.strip()]

    @model_validator(mode="after")
    def _shaped(self) -> "Reply":
        count = len(self.attachments) if type(self)._attaches else None
        if found := type(self).style.problems(self.bubbles, count):
            raise ValueError(" ".join(found))
        return self

    def outbound(self, ctx: Any = None) -> list[Outbound]:
        """The bubbles as text messages, then each attachment's messages in the order the model
        gave them: what a turn's handler returns. `ctx` is the app's own (a database session, who
        wrote) and goes to each attachment. One that resolves to nothing, or fails, is left out
        and logged (its type only); the rest still go."""
        out = [Outbound.text_message(b) for b in self.bubbles]
        for a in self.attachments:
            try:
                messages = a.to_outbound(ctx)
            except Exception:
                log.exception("attachment %s failed; left out", a.type)
                continue
            if not messages:
                log.warning("attachment %s resolved to nothing; left out", a.type)
            out += messages
        return out


def _attachment_types(reply: type[Reply]) -> list[type[Attachment]]:
    found: list[type[Attachment]] = []

    def walk(t: Any) -> None:
        if isinstance(t, type) and issubclass(t, Attachment):
            found.append(t)
        for arg in get_args(t):
            walk(arg)

    if reply._attaches:
        walk(reply.model_fields["attachments"].annotation)
    return found


def undescribed(reply: type[Reply]) -> list[str]:
    """What the model would read without a word on it: fields of `reply` and of its attachment
    types with no `description`, attachment types with no `when`. Each as a sentence saying
    what to add. Empty when everything is described."""
    found = []
    for name, info in reply.model_fields.items():
        if name == "attachments" and not reply._attaches:
            continue
        if not (info.description or "").strip():
            found.append(
                f"{reply.__name__}.{name} has no description: say what the model puts here and "
                'when, e.g. Field(description="...").'
            )
    for t in _attachment_types(reply):
        if not t.when.strip():
            found.append(
                f"{t.__name__} has no `when`: say when the model should add it, e.g. "
                'when = "When they ask where the venue is or how to get there."'
            )
        for name, info in t.model_fields.items():
            if name != "type" and not (info.description or "").strip():
                found.append(
                    f"{t.__name__}.{name} has no description: say what the model writes here "
                    "and where it comes from, e.g. Field(description=\"the venue's slug, from "
                    'a tool result").'
                )
    return found


def assert_described(reply: type[Reply]) -> None:
    """For your tests: fails listing every field and attachment type the model would read
    without a description (`undescribed`). The model picks an attachment, and fills a field,
    from what these say."""
    if found := undescribed(reply):
        raise AssertionError("\n".join(found))
