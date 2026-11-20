"""Read models: scoped by the principal, redacted and audited, and their HTTP surface."""

import uuid
from decimal import Decimal

import pytest
from conftest import T0, FakeChannel, msg, receive, runtime
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meydan.auth import AuthConfig, Principal, grant, sign_in_interceptor
from meydan.auth.web import Auth
from meydan.channels.base import Outbound
from meydan.observe import Usage, query, record_usage
from meydan.observe.query import NotAllowedError
from meydan.observe.web import routes as observe_routes
from meydan.visibility import Rule, Visibility

ANNA, BORA = "+15550002001", "+15550002002"  # fake numbers: this is a public repository


async def agent(turn):
    if "fail" in turn.text:
        raise RuntimeError("tool exploded")
    record_usage(
        Usage(
            "openrouter",
            "small-model",
            input_tokens=100,
            output_tokens=10,
            cost_usd=Decimal("0.0002"),
            cost_source="provider",
        )
    )
    return [Outbound.text_message(f"re: {turn.text}")]


def actor_of(rt, address):
    t = rt.t
    with rt.engine.connect() as c:
        return c.scalar(
            select(t.claims.c.actor_id)
            .join(t.identities, t.identities.c.id == t.claims.c.identity_id)
            .where(t.identities.c.value == address)
        )


def who(actor_id, *scopes):
    return Principal(actor_id, uuid.uuid4(), "ops", frozenset(scopes))


@pytest.fixture
async def world(db):
    rt = runtime(db, agent, FakeChannel(), max_turn_attempts=1)
    receive(rt, [msg("a1", "merhaba", T0, sender=ANNA), msg("b1", "selam", T0, sender=BORA)])
    await rt.work_once()
    receive(rt, [msg("b2", "please fail", T0, sender=BORA)])
    await rt.work_once()
    return rt, actor_of(rt, ANNA), actor_of(rt, BORA)


def read(rt, fn, *args, **kwargs):
    with rt.engine.begin() as c:
        return fn(c, rt.t, *args, **kwargs)


async def test_overview_needs_ops_and_counts_turns_and_cost(world):
    rt, anna, _ = world
    with pytest.raises(NotAllowedError):
        read(rt, query.overview, who(anna))
    assert read(rt, query.overview, who(anna, "ops")).cost_usd is None  # money needs costs:read
    o = read(rt, query.overview, who(anna, "ops", "costs:read"))
    assert (o.turns, o.replied, o.gave_up) == (3, 2, 1)
    assert o.cost_usd == Decimal("0.0004") and o.cost_exact_share == 1.0
    assert o.messages_in == 3 and o.active_conversations == 2


async def test_people_see_only_their_own_conversations_masked(world):
    rt, anna, _ = world
    own = read(rt, query.conversations, who(anna))
    assert [c.actor_id for c in own] == [anna] and own[0].address == "+15 •••••••01"
    everyone = read(rt, query.conversations, who(anna, "conversations:read:all"))
    assert len(everyone) == 2


async def test_text_of_others_is_redacted_unless_allowed_and_then_audited(world):
    rt, anna, bora = world
    bora_conv = read(rt, query.conversations, who(bora))[0].id
    assert read(rt, query.conversation, who(anna), bora_conv) is None  # not even its existence

    mine = read(rt, query.conversation, who(bora), bora_conv)
    assert [line.text for line in mine.lines if line.direction == "in"] == ["selam", "please fail"]

    ops = who(anna, "conversations:read:all", "ops", "costs:read")
    redacted = read(rt, query.conversation, ops, bora_conv)
    assert redacted.text_redacted and all(line.text is None for line in redacted.lines)
    assert [t.status for t in redacted.turns] == ["replied", "gave_up"]
    assert redacted.turns[0].cost_usd == Decimal("0.0002") and redacted.turns[0].typing_at

    with rt.engine.connect() as c:
        assert c.scalar(select(rt.t.audit.c.id)) is None  # redacted reads aren't audited
    reader = who(anna, "conversations:read:all", "messages:read")
    full = read(rt, query.conversation, reader, bora_conv)
    assert not full.text_redacted
    with rt.engine.connect() as c:
        audit = c.execute(select(rt.t.audit)).one()
    assert (audit.actor_id, audit.action, audit.subject_id) == (anna, "read:messages", bora_conv)


async def test_agent_details_and_costs_hidden_without_their_scopes(world):
    rt, anna, bora = world
    bora_conv = read(rt, query.conversations, who(bora))[0].id
    plain = read(rt, query.conversation, who(anna, "conversations:read:all"), bora_conv)
    assert plain.details_hidden and plain.turns == []
    rows = read(rt, query.conversations, who(anna, "conversations:read:all"))
    assert all(r.cost_usd is None for r in rows)


async def test_addresses_in_full_only_with_their_scope_and_audited(world):
    rt, anna, _ = world
    rows = read(rt, query.conversations, who(anna, "conversations:read:all", "addresses:read"))
    assert sorted(r.address for r in rows) == sorted([ANNA, BORA])
    with rt.engine.connect() as c:
        assert c.scalar(select(rt.t.audit.c.action)) == "read:addresses"


async def test_trusted_preset_shows_everything_to_the_console_scope_and_still_audits(world):
    rt, anna, bora = world
    trusted = Visibility.trusted("ops")
    bora_conv = read(rt, query.conversations, who(bora))[0].id
    team = who(anna, "ops")
    full = read(rt, query.conversation, team, bora_conv, visibility=trusted)
    assert not full.text_redacted and not full.details_hidden
    assert full.turns[0].cost_usd == Decimal("0.0002")
    assert read(rt, query.costs, team, visibility=trusted).total_usd == Decimal("0.0004")
    with rt.engine.connect() as c:
        assert c.scalar(select(rt.t.audit.c.action)) == "read:messages"


def test_overrides_keep_audit_and_reject_unknown_items():
    v = Visibility.need_to_know(message_text="team:read")
    assert v.message_text == Rule("team:read", audit=True)
    assert Visibility.trusted(costs=Rule("money")).costs == Rule("money")
    with pytest.raises(ValueError, match="unknown visibility item"):
        Visibility.need_to_know(messages="x")


async def test_costs_and_problems(world):
    rt, anna, _ = world
    with pytest.raises(NotAllowedError):
        read(rt, query.costs, who(anna))
    report = read(rt, query.costs, who(anna, "costs:read"))
    assert report.total_usd == Decimal("0.0004") and report.models[0].model == "small-model"
    issues = read(rt, query.problems, who(anna, "ops"))
    assert [p.kind for p in issues] == ["turn_gave_up"] and "tool exploded" in issues[0].error


async def test_http_surface_401_403_404(world):
    rt, anna, _ = world
    rt.intercept = [sign_in_interceptor(rt.t)]
    auth = Auth(rt.engine, rt.t, AuthConfig(audience="ops"), secure_cookies=False)
    app = FastAPI()
    app.include_router(auth.routes(), prefix="/auth")
    app.include_router(observe_routes(auth), prefix="/observe")
    client = TestClient(app)
    assert client.get("/observe/overview").status_code == 401

    with rt.engine.begin() as c:
        grant(c, rt.t, anna, "ops")
    code = client.post("/auth/sign-in").json()["code"]
    receive(rt, [msg("a9", code, T0, sender=ANNA)])
    assert client.get("/auth/sign-in").json()["status"] == "signed_in"

    assert client.get("/observe/overview").json()["turns"] == 3
    assert client.get("/observe/costs").status_code == 403
    assert client.get(f"/observe/conversations/{uuid.uuid4()}").status_code == 404
    assert "/observe/overview" in client.get("/openapi.json").json()["paths"]


async def test_who_wrote_each_line_is_recorded_not_guessed(world):
    """A turn id is no proof the agent wrote it (a reminder gets one too): the author is kept."""
    rt, anna, bora = world
    anna_conv = read(rt, query.conversations, who(anna))[0].id
    staff = actor_of(rt, BORA)  # any actor stands in for a member of staff
    with rt.engine.begin() as c:
        for author, text_, by in (("system", "reminder", None), ("staff", "from a person", staff)):
            rt.enqueue(c, channel="whatsapp", conversation_id=anna_conv, from_number="15550000000",
                       to=ANNA, messages=[Outbound.text_message(text_)], author=author,
                       author_actor_id=by)  # fmt: skip
    lines = read(rt, query.conversation, who(anna), anna_conv).lines
    assert [(ln.direction, ln.author, ln.text) for ln in lines] == [
        ("in", "person", "merhaba"),
        ("out", "agent", "re: merhaba"),
        ("out", "system", "reminder"),
        ("out", "staff", "from a person"),
    ]
    assert lines[-1].author_id == staff and lines[1].author_id is None
    assert lines[1].status in ("pending", "sent") and lines[0].status is None
    assert len({ln.id for ln in lines}) == 4  # each line has its own id


async def test_the_list_shows_the_last_message_and_who_wrote_it_redacted_like_the_thread(world):
    rt, anna, _ = world
    mine = read(rt, query.conversations, who(anna))[0]
    assert (mine.last_text, mine.last_direction, mine.last_author) == (
        "re: merhaba",
        "out",
        "agent",
    )
    assert mine.last_message_at >= mine.last_at  # the last message, either way; last_at: theirs
    others = read(rt, query.conversations, who(anna, "conversations:read:all"))
    bora_row = next(r for r in others if r.actor_id != anna)
    assert bora_row.last_text is None and bora_row.last_author is not None  # text needs its scope
    full = read(rt, query.conversations, who(anna, "conversations:read:all", "messages:read"))
    assert next(r for r in full if r.actor_id != anna).last_text is not None


async def test_a_template_shows_what_it_said_and_a_code_never_shows(world):
    from meydan.channels.base import TemplateUpdate
    from meydan.runtime import templates as tpl

    rt, anna, _ = world
    conv = read(rt, query.conversations, who(anna))[0].id
    with rt.engine.begin() as c:
        for name, category, body in (
            ("reminder", "UTILITY", "Your {{group}} lesson is at {{time}}. Coming?"),
            ("signin", "AUTHENTICATION", "{{1}} is your code."),
        ):
            update = TemplateUpdate(
                "whatsapp", "waba", name, "tr", T0,
                status="APPROVED", category=category, body=body, buttons=("Yes", "No"),
            )  # fmt: skip
            tpl.record(c, rt.t, update)
        reminder = Outbound.template_message("reminder", "tr", buttons=("y", "n"),
                                             group="Mini", time="18:00")  # fmt: skip
        code = Outbound.code_message("signin", "tr", "123456")
        rt.enqueue(c, channel="whatsapp", conversation_id=conv, from_number="15550000000",
                   to=ANNA, messages=[reminder, code])  # fmt: skip
    lines = read(rt, query.conversation, who(anna), conv).lines
    shown = [(ln.template, ln.text, ln.buttons) for ln in lines if ln.template]
    assert shown == [
        ("reminder", "Your Mini lesson is at 18:00. Coming?", ["Yes", "No"]),
        ("signin", None, []),  # a code is never shown, not even its template's words
    ]


async def test_each_line_shows_its_parts_as_the_person_saw_them(world):
    from sqlalchemy import update

    from meydan import media
    from meydan.channels.base import Address, Choice, Inbound

    rt, anna, bora = world
    conv = read(rt, query.conversations, who(anna))[0].id
    with rt.engine.begin() as c:
        key = media.put(c, rt.t, b"\x89PNG poster", "image/png")
        rt.enqueue(c, channel="whatsapp", conversation_id=conv, from_number="15550000000",
                   to=ANNA, messages=[
            Outbound.image_message(key, alt="Spring Cup poster"),
            Outbound.location_message(41.0, 29.0, place="Court One", address="1 Main St"),
            Outbound.choices_message("Which one?", [Choice("a", "Spring Cup"),
                                                   Choice("b", "Autumn Cup")], label="Pick"),
            Outbound.contact_message("Ada Kaya", "+15550009999", title="Sales"),
        ])  # fmt: skip
        o = rt.t.outbox
        c.execute(update(o).where(o.c.message["kind"].astext == "choices")
                  .values(channel_message_id="wamid.ASK"))  # fmt: skip
    pressed = {"type": "button_reply", "button_reply": {"id": "a", "title": "Spring Cup"}}
    tap = Inbound(
        "whatsapp", "wamid.TAP", Address("wa-pnid", "222"), Address("tel", ANNA), T0,
        "interactive", "Spring Cup", {"interactive": pressed}, reply_to="wamid.ASK",
    )  # fmt: skip
    receive(rt, [tap])

    lines = read(rt, query.conversation, who(anna), conv).lines
    by_kind = {ln.kind: ln for ln in lines}
    image = by_kind["image"]
    assert (image.image.media, image.image.alt, image.text) == (key, "Spring Cup poster", None)
    pin = by_kind["location"]
    assert (pin.location.lat, pin.location.name, pin.text) == (41.0, "Court One", None)
    ask = by_kind["choices"]
    assert [o.title for o in ask.choices.options] == ["Spring Cup", "Autumn Cup"]
    assert (ask.choices.question, ask.choices.style, ask.choices.label) == (
        "Which one?", "buttons", "Pick")  # fmt: skip
    card = by_kind["contact"]
    assert (card.contact.name, card.contact.phone, card.contact.title) == (
        "Ada Kaya", "+15550009999", "Sales")  # fmt: skip
    answer = by_kind["interactive"]
    assert answer.tapped and answer.reply_to == ask.id and answer.text == "Spring Cup"

    hidden = read(rt, query.conversation, who(bora, "ops"), conv)  # may not read others' messages
    if hidden is not None:
        assert all(
            ln.image is ln.location is ln.choices is ln.contact is None for ln in hidden.lines
        )


async def test_a_file_we_sent_is_shown_like_its_conversation(world):
    from meydan import media

    rt, anna, bora = world
    conv = read(rt, query.conversations, who(anna))[0].id
    with rt.engine.begin() as c:
        key = media.put(c, rt.t, b"\x89PNG poster", "image/png")
        rt.enqueue(c, channel="whatsapp", conversation_id=conv, from_number="15550000000",
                   to=ANNA, messages=[Outbound.image_message(key)])  # fmt: skip
    assert read(rt, query.media, who(anna), key) == (b"\x89PNG poster", "image/png")  # hers
    assert read(rt, query.media, who(bora), key) is None  # not his conversation
    reader = who(bora, "messages:read")  # may read others' messages: shown, and audited
    assert read(rt, query.media, reader, key)[0] == b"\x89PNG poster"
    with rt.engine.connect() as c:
        actions = c.execute(select(rt.t.audit.c.action)).scalars().all()
    assert "read:media" in actions
    assert read(rt, query.media, who(anna), "0" * 64) is None  # no such file
