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
from meydan.rules import DEFAULT


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


def test_a_channel_that_counts_bytes_gets_bubbles_that_fit_in_bytes():
    style = ReplyStyle(max_chars=None, max_bytes=DEFAULT.of("instagram").text_bytes_max)
    assert style.problems(["ş" * 500]) == []  # 1000 bytes
    [problem] = style.problems(["ş" * 501])
    assert problem.startswith("Bubble 1 is too long for this channel (1002 bytes; at most 1000)")


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


# --- Attachments ------------------------------------------------------------------------------

from typing import Literal  # noqa: E402

from pydantic import Field  # noqa: E402

from meydan.reply import (  # noqa: E402
    Attachments,
    Choices,
    Contact,
    Image,
    ImageRef,
    Link,
    LinkRef,
    Location,
    Person,
    Place,
    assert_described,
    undescribed,
)

PLACES = {"court": Place(41.0, 29.0, "Court One", "1 Main St", url="https://maps.example/c1")}


class Venue(Location):
    type: Literal["location"] = "location"
    when = "When they ask where it is or how to get there."
    venue: str = Field(description="The venue's slug, from a tool result.")

    def resolve(self, ctx):
        return PLACES.get(self.venue)


class Poster(Image):
    type: Literal["poster"] = "poster"
    when = "When they ask about an event and haven't seen its poster yet."
    event: str = Field(description="The event's id, from a tool result.")

    def resolve(self, ctx):
        return ImageRef("k" * 64, alt=f"{self.event} poster") if self.event == "spring" else None


class Desk(Contact):
    type: Literal["desk"] = "desk"
    when = "When they want to talk to a person."
    purpose: str = Field(description="What they want to talk about: 'sales'.")

    def resolve(self, ctx):
        return Person("Ada Kaya", "+905550000002", title="Sales") if ctx == "ok" else None


class PayLink(Link):
    type: Literal["pay"] = "pay"
    when = "When they want to pay for their order."
    order: str = Field(description="The order's id, from a tool result.")

    def resolve(self, ctx):
        if self.order != "o1":
            return None
        return LinkRef("https://pay.example/o/secret", "Pay", "Order o1: ₺500", ref="order:o1")


class Broken(Location):
    type: Literal["broken"] = "broken"
    when = "Never."

    def resolve(self, ctx):
        raise RuntimeError("database down")


class Turkish(Choices):
    label = "Seç"


class Full(Reply):
    attachments: Attachments[Venue, Poster, Desk, Turkish, PayLink, Broken] = Field(
        default_factory=list, description="Sent after the bubbles, in this order."
    )


def test_attachments_go_after_the_bubbles_in_the_models_order():
    reply = Full(
        bubbles=["Burası."],
        attachments=[
            {"type": "poster", "event": "spring"},
            {"type": "location", "venue": "court"},
            {"type": "desk", "purpose": "sales"},
        ],
    )
    out = reply.outbound("ok")
    assert [m.kind for m in out] == ["text", "image", "location", "text", "contact"]
    assert out[1].text == "[image: spring poster]"
    assert (out[2].lat, out[2].place, out[3].text) == (41.0, "Court One", "https://maps.example/c1")
    assert (out[4].contact_name, out[4].contact_phone) == ("Ada Kaya", "+905550000002")


def test_a_reply_can_be_attachments_alone_but_not_nothing():
    assert [
        m.kind for m in Full(attachments=[{"type": "location", "venue": "court"}]).outbound()
    ] == [
        "location",
        "text",
    ]
    with pytest.raises(ValidationError, match="at least one bubble with text, or an attachment"):
        Full()
    with pytest.raises(ValidationError, match="at least one bubble with text\\."):
        Reply()  # a reply that carries no attachments says so without them
    with pytest.raises(ValidationError, match="4 attachments; at most 3"):
        Full(attachments=[{"type": "location", "venue": "court"}] * 4)


def test_one_that_resolves_to_nothing_or_fails_is_left_out_and_the_rest_go(caplog):
    reply = Full(
        bubbles=["Bak."],
        attachments=[
            {"type": "location", "venue": "made-up"},
            {"type": "broken"},
            {"type": "poster", "event": "spring"},
        ],
    )
    assert [m.kind for m in reply.outbound()] == ["text", "image"]
    assert "location resolved to nothing" in caplog.text and "broken failed" in caplog.text
    assert "made-up" not in caplog.text  # the type only, never what the model wrote


def test_choices_are_buttons_or_a_list_and_text_when_they_cant_be():
    two = Full(attachments=[{"type": "choices", "question": "Onay?", "options": [
        {"title": "Evet"}, {"title": "Düzelt"}]}])  # fmt: skip
    [m] = two.outbound()
    assert m.kind == "choices" and m.choices_label == "Seç"
    assert [c.title for c in m.choices] == ["Evet", "Düzelt"]
    same = Full(attachments=[{"type": "choices", "question": "Hangisi?", "options": [
        {"title": "A"}, {"title": "a"}]}])  # fmt: skip
    [m] = same.outbound()
    assert m.kind == "text" and m.text == "Hangisi?\n• A\n• a"
    with pytest.raises(ValidationError):  # WhatsApp's own limits are in the schema
        Full(attachments=[{"type": "choices", "question": "Q", "options": [{"title": "x" * 25}]}])


def test_a_link_is_built_in_code_and_its_address_never_reaches_a_transcript():
    [m] = Full(bubbles=["Ödeme sayfanız hazır."], attachments=[{"type": "pay", "order": "o1"}]
               ).outbound()[1:]  # fmt: skip
    assert (m.kind, m.url, m.url_label, m.body, m.ref) == (
        "link", "https://pay.example/o/secret", "Pay", "Order o1: ₺500", "order:o1")  # fmt: skip
    assert m.text == "Order o1: ₺500\n[link: Pay]"  # what an agent reads of it later
    # If the channel refuses the button for good, the text with the address goes instead; an
    # agent still reads it without the address.
    assert m.fallback.kind == "text" and m.fallback.ref == "order:o1"
    assert m.fallback.text.endswith("Pay: https://pay.example/o/secret")
    assert "secret" not in m.fallback.recall
    assert Full(attachments=[{"type": "pay", "order": "made-up"}]).outbound() == []
    with pytest.raises(ValidationError):  # the model names the order; it can't give an address
        Full(attachments=[{"type": "pay", "order": "o1", "url": "https://elsewhere.example"}])
    schema = Full.model_json_schema()["$defs"]["PayLink"]
    assert set(schema["properties"]) == {"type", "order"}


def test_the_schema_tells_attachments_apart_by_type_and_says_when_to_use_each():
    schema = Full.model_json_schema()
    items = schema["properties"]["attachments"]
    assert items["maxItems"] == 3 and items["items"]["discriminator"]["propertyName"] == "type"
    assert schema["$defs"]["Venue"]["description"] == Venue.when  # `when`, not the docstring
    assert schema["$defs"]["Venue"]["properties"]["venue"]["description"].startswith("The venue")
    assert schema["properties"]["bubbles"]["minItems"] == 0
    assert "attachments" not in Reply.model_json_schema()["properties"]  # only once you add some


def test_assert_described_names_what_the_model_would_read_without_a_word():
    assert_described(Full)

    class Bare(Location):
        type: Literal["bare"] = "bare"
        venue: str

    class Loose(Reply):
        attachments: Attachments[Bare] = Field(default_factory=list)
        pin: str | None = None

    found = undescribed(Loose)
    assert any("Loose.attachments has no description" in f for f in found)
    assert any("Loose.pin has no description" in f for f in found)
    assert any("Bare has no `when`" in f for f in found)
    assert any("Bare.venue has no description" in f for f in found)
    with pytest.raises(AssertionError, match="Bare.venue"):
        assert_described(Loose)


async def test_a_model_picks_an_attachment_by_its_type():
    def model(messages, info: AgentInfo) -> ModelResponse:
        [output] = info.output_tools
        return ModelResponse(
            parts=[
                ToolCallPart(output.name, {"attachments": [{"type": "location", "venue": "court"}]})
            ]
        )

    result = await Agent(FunctionModel(model), output_type=Full).run("nerede?")
    assert [m.kind for m in result.output.outbound()] == ["location", "text"]


# --- Declared from a table (.of) --------------------------------------------------------------

import uuid  # noqa: E402

from sqlalchemy import ForeignKey, create_engine  # noqa: E402
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column  # noqa: E402


class Base(DeclarativeBase):
    pass


class Club(Base):
    __tablename__ = "clubs"
    slug: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    address: Mapped[str | None]
    lat: Mapped[float | None]
    lng: Mapped[float | None]
    maps_url: Mapped[str | None]


class Event(Base):
    __tablename__ = "events"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str]
    poster: Mapped[str | None]
    public: Mapped[bool] = mapped_column(default=True)


class Card(Base):
    __tablename__ = "cards"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[str]
    purpose: Mapped[str]
    name: Mapped[str]
    phone: Mapped[str]
    title: Mapped[str | None]
    club: Mapped[str | None] = mapped_column(ForeignKey("clubs.slug"))


Place_ = Location.of(
    Club, by=Club.slug, field="venue", type="venue",
    when="When they ask where it is.", describe="The venue's slug, from a tool result.",
    lat=Club.lat, lng=Club.lng, name=Club.name, address=Club.address, url=Club.maps_url,
)  # fmt: skip
EventPoster = Image.of(
    Event, by=Event.id, field="event", type="poster",
    when="When they ask about an event.", describe="The event's id, from a tool result.",
    media=Event.poster, alt="{name} poster", visible=lambda ctx, e: e.public,
)  # fmt: skip
SalesDesk = Contact.of(
    Card, by=Card.purpose, type="desk", when="When they want to buy.",
    describe="What it is about: 'sales'.", name=Card.name, phone=Card.phone, title=Card.title,
    where={Card.owner: "platform"},
)  # fmt: skip


Tickets = Link.of(
    Event, by=Event.id, field="event", type="tickets", when="When they want to buy tickets.",
    describe="The event's id, from a tool result.", label="Buy",
    url=lambda e: f"https://tickets.example/{e.id}" if e.public else None, text="{name} tickets",
)  # fmt: skip


class Declared(Reply):
    attachments: Attachments[Place_, EventPoster, SalesDesk, Tickets] = Field(
        default_factory=list, description="Sent after the bubbles, in this order."
    )


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_declared_attachments_find_their_row_and_send_it(session):
    shown = Event(name="Spring Cup", poster="k" * 64)
    hidden = Event(name="Test Cup", poster="k" * 64, public=False)
    session.add_all([
        Club(slug="court", name="Court One", address="1 Main St", lat=41.0, lng=29.0,
             maps_url="https://maps.example/c1"),
        Club(slug="nowhere", name="No Pin"),
        shown, hidden,
        Card(owner="other", purpose="sales", name="Not Them", phone="+905550000009"),
        Card(owner="platform", purpose="sales", name="Ada Kaya", phone="+905550000002",
             title="Sales"),
    ])  # fmt: skip
    session.flush()
    reply = Declared(attachments=[
        {"type": "poster", "event": str(shown.id)},
        {"type": "venue", "venue": "court"},
        {"type": "desk", "purpose": "sales"},
    ])  # fmt: skip
    out = reply.outbound(session)
    assert [m.kind for m in out] == ["image", "location", "text", "contact"]
    assert out[0].text == "[image: Spring Cup poster]"
    assert (out[1].place, out[2].text) == ("Court One", "https://maps.example/c1")
    assert out[3].contact_name == "Ada Kaya"  # `where` picked the platform's card

    nothing = Declared(attachments=[
        {"type": "venue", "venue": "nowhere"},     # no coordinates
        {"type": "venue", "venue": "made-up"},     # no such row
        {"type": "poster", "event": str(hidden.id)},  # not visible
    ])  # fmt: skip
    assert nothing.outbound(session) == []
    garbled = Declared(attachments=[{"type": "poster", "event": "not-a-uuid"}])
    assert garbled.outbound(session) == []  # not a key at all


def test_a_declared_link_builds_its_address_from_the_row(session):
    shown = Event(name="Spring Cup", poster=None)
    hidden = Event(name="Test Cup", poster=None, public=False)
    session.add_all([shown, hidden])
    session.flush()
    [m] = Declared(attachments=[{"type": "tickets", "event": str(shown.id)}]).outbound(session)
    assert (m.kind, m.url, m.url_label, m.body) == (
        "link", f"https://tickets.example/{shown.id}", "Buy", "Spring Cup tickets")  # fmt: skip
    # no address for this row: nothing is sent
    assert (
        Declared(attachments=[{"type": "tickets", "event": str(hidden.id)}]).outbound(session) == []
    )


def test_a_declared_type_reads_like_one_written_by_hand():
    assert_described(Declared)
    schema = Declared.model_json_schema()["$defs"]
    venue = next(d for d in schema.values() if d["properties"]["type"].get("const") == "venue")
    assert venue["description"] == "When they ask where it is."
    assert venue["properties"]["venue"]["description"] == "The venue's slug, from a tool result."
    assert "found by slug" in Place_.__doc__ and "lat=lat" in Place_.__doc__


def test_columns_of_another_table_fail_when_the_type_is_made():
    with pytest.raises(TypeError, match="lat=.* is not a column of Club"):
        Location.of(Club, by=Club.slug, type="x", lat=Event.name, lng=Club.lng)


def test_a_declared_type_needs_a_session_and_says_so():
    with pytest.raises(TypeError, match="SQLAlchemy Session"):
        Place_(venue="court").resolve(object())
