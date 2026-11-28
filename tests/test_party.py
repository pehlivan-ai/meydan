"""meydan.party: people in something together, each in a seat, and the three doors to a seat.

A real Postgres; the app's kind is a "duo" (exactly two) whose handler records what it hears.
Test data is fake: this is a public repository.
"""

from dataclasses import replace

import pytest
from conftest import T0, FakeChannel, msg, receive, unavailable
from sqlalchemy import MetaData, create_engine, select, text

from meydan import party as pt
from meydan.channels.base import Address, Outbound
from meydan.runtime import Runtime, RuntimeConfig, define_tables
from meydan.runtime.expect import Expectations
from meydan.runtime.identity import new_actor

HOST_PHONE, GUEST_PHONE, OTHER_PHONE = "+905550000101", "+905550000102", "+905550000103"
ENDPOINT = Address("wa-pnid", "222")


class Duo:
    def __init__(self):
        self.heard: list[tuple] = []

    def on_accepted(self, conn, party, seat, door):
        self.heard.append(("accepted", seat.id, str(door)))
        return [Outbound.text_message("welcome")]

    def on_released(self, conn, party, seat):
        self.heard.append((str(seat.state), seat.id))
        return [Outbound.text_message("closed")]


@pytest.fixture
def world():
    engine = create_engine(pt_url())
    try:
        with engine.connect() as c:
            c.execute(text("select 1"))
    except Exception as e:
        unavailable(f"no Postgres test database: {e}")
    md = MetaData()
    t = define_tables(md)
    tables = pt.define_tables(md)
    md.drop_all(engine)
    md.create_all(engine)
    duo = Duo()
    parties = pt.Parties(
        t, tables, {"demo.duo": pt.Kind("demo.duo", pt.Capacity.exactly(2), duo)},
    )  # fmt: skip
    cfg = RuntimeConfig(debounce_s=0, max_wait_s=0, chars_per_second=1e9, presence="instant")
    rt = Runtime(
        engine, t, FakeChannel(), lambda turn: [], cfg,
        expect=Expectations(t, {pt.KIND: parties.handler()}),
    )  # fmt: skip
    yield rt, parties, duo
    md.drop_all(engine)
    engine.dispose()


def pt_url():
    from conftest import URL

    return URL


def actor(rt):
    with rt.engine.begin() as c:
        return new_actor(c, rt.t, "person")


def facts(rt, kind):
    e = rt.t.events
    with rt.engine.connect() as c:
        return c.scalars(select(e.c.subject_id).where(e.c.kind == kind)).all()


def made(rt, parties, host):
    with rt.engine.begin() as c:
        party, _ = parties.create(c, "demo.duo", subject="match:1", by=host)
        seat = parties.invite(
            c, party.id, by=host, name="Guest", address=Address("tel", GUEST_PHONE)
        )
    return party, seat


def test_a_party_starts_with_its_creator_and_invites_count_against_its_ceiling(world):
    rt, parties, _ = world
    host = actor(rt)
    party, seat = made(rt, parties, host)
    assert party.state == pt.PartyState.FORMING and party.capacity == pt.Capacity(2, 2)
    assert seat.state == pt.SeatState.INVITED and len(seat.code) == pt.CODE_LENGTH
    with rt.engine.begin() as c, pytest.raises(pt.SeatError) as e:
        parties.invite(c, party.id, by=host, name="Third")
    assert e.value.reason == "full"
    assert facts(rt, "seat.invited") == [seat.id]


def test_the_code_door_takes_the_seat_once_and_confirms_the_party(world):
    rt, parties, duo = world
    host, guest, other = actor(rt), actor(rt), actor(rt)
    party, seat = made(rt, parties, host)
    assert pt.find_code(f"Joining #{seat.code.lower()} now") == seat.code
    with rt.engine.begin() as c:
        out = parties.redeem(c, seat.code, guest)
    assert out.seat.state == pt.SeatState.ACCEPTED and out.seat.door == pt.Door.CODE
    assert out.party.state == pt.PartyState.CONFIRMED
    assert [r.text for r in out.replies] == ["welcome"]
    assert duo.heard == [("accepted", seat.id, "code")]
    assert facts(rt, "party.confirmed") == [party.id]
    with rt.engine.begin() as c, pytest.raises(pt.SeatError) as e:
        parties.redeem(c, seat.code, other)
    assert e.value.reason == "closed"


def test_nobody_takes_their_own_invite_or_a_second_seat_of_the_same_subject(world):
    rt, parties, _ = world
    host, guest = actor(rt), actor(rt)
    _, seat = made(rt, parties, host)
    with rt.engine.begin() as c, pytest.raises(pt.SeatError) as e:
        parties.accept(c, seat.id, host, pt.Door.CLAIM)
    assert e.value.reason == "own"
    with rt.engine.begin() as c:
        parties.accept(c, seat.id, guest, pt.Door.CLAIM)
    other_host = actor(rt)
    _, second = made(rt, parties, other_host)
    with rt.engine.begin() as c, pytest.raises(pt.SeatError) as e:
        parties.accept(c, second.id, guest, pt.Door.CLAIM)
    assert e.value.reason == "already_in"


def test_the_claim_door_finds_the_inviters_open_seats(world):
    rt, parties, _ = world
    host = actor(rt)
    _, seat = made(rt, parties, host)
    with rt.engine.connect() as c:
        assert [s.id for s in parties.invited_by(c, "demo.duo", "match:1", host)] == [seat.id]
        assert parties.invited_by(c, "demo.duo", "match:2", host) == []


async def test_the_address_door_asks_with_buttons_and_a_press_takes_the_seat(world):
    rt, parties, duo = world
    host = actor(rt)
    _, seat = made(rt, parties, host)
    with rt.engine.begin() as c:
        exp = parties.ask(
            c, rt, seat.id, channel="whatsapp", endpoint=ENDPOINT,
            message=lambda b: Outbound.template_message(
                "duo_invite", "en", buttons=(b["accept"], b["decline"])
            ),
        )  # fmt: skip
    await rt.send_once()
    o = rt.t.outbox
    with rt.engine.connect() as c:
        row = c.execute(select(o).where(o.c.turn_id == exp.turn_id)).one()
        assert parties.seat(c, seat.id).expectation_id == exp.id
    accept = row.message["buttons"][0]
    pressed = replace(msg("in1", "Accept", T0, sender=GUEST_PHONE), kind="button",
                      reply_to=row.channel_message_id)  # fmt: skip
    pressed = replace(pressed, payload={"id": "in1", "button": {"payload": accept}})
    receive(rt, [pressed])
    with rt.engine.connect() as c:
        taken = parties.seat(c, seat.id)
        said = c.scalars(select(o.c.message["text"].astext).order_by(o.c.created_at.desc())).first()
    assert taken.state == pt.SeatState.ACCEPTED and taken.door == pt.Door.ADDRESS
    assert taken.actor_id is not None and said == "welcome"
    assert duo.heard[-1][0] == "accepted"


async def test_a_press_from_another_number_is_refused_and_decline_releases(world):
    rt, parties, duo = world
    host = actor(rt)
    _, seat = made(rt, parties, host)
    with rt.engine.begin() as c:
        exp = parties.ask(
            c, rt, seat.id, channel="whatsapp", endpoint=ENDPOINT,
            message=lambda b: Outbound.template_message(
                "duo_invite", "en", buttons=(b["accept"], b["decline"])
            ),
        )  # fmt: skip
    await rt.send_once()
    o = rt.t.outbox
    with rt.engine.connect() as c:
        row = c.execute(select(o).where(o.c.turn_id == exp.turn_id)).one()
    accept, decline = row.message["buttons"]
    for mid, sender, button in (("x1", OTHER_PHONE, accept), ("x2", GUEST_PHONE, decline)):
        m = replace(msg(mid, "b", T0, sender=sender), kind="button",
                    reply_to=row.channel_message_id)  # fmt: skip
        receive(rt, [replace(m, payload={"id": mid, "button": {"payload": button}})])
    with rt.engine.connect() as c:
        assert parties.seat(c, seat.id).state == pt.SeatState.DECLINED
    assert duo.heard == [("declined", seat.id)]
    assert facts(rt, "seat.declined") == [seat.id]


def test_an_invitation_past_its_time_expires_when_taken_late(world):
    rt, parties, duo = world
    host, guest = actor(rt), actor(rt)
    _, seat = made(rt, parties, host)
    s = parties.pt.seats
    with rt.engine.begin() as c:
        c.execute(s.update().where(s.c.id == seat.id).values(expires_at=T0))
    with rt.engine.begin() as c, pytest.raises(pt.SeatError) as e:
        parties.accept(c, seat.id, guest, pt.Door.CODE)
    assert e.value.reason == "expired"
    with rt.engine.connect() as c:
        assert parties.seat(c, seat.id).state == pt.SeatState.EXPIRED
    assert duo.heard == [("expired", seat.id)]


def test_a_taken_seat_can_be_left(world):
    rt, parties, duo = world
    host, guest = actor(rt), actor(rt)
    _, seat = made(rt, parties, host)
    with rt.engine.begin() as c:
        parties.accept(c, seat.id, guest, pt.Door.CODE)
        out = parties.leave(c, seat.id, guest)
    assert out.seat.state == pt.SeatState.LEFT
    assert facts(rt, "seat.left") == [seat.id]


async def test_ask_in_a_conversation_and_one_door_closes_the_others(world):
    rt, parties, _ = world
    host = actor(rt)
    _, seat = made(rt, parties, host)
    receive(rt, [msg("g1", "hello", T0, sender=GUEST_PHONE)])  # the guest writes to us
    inbox = rt.t.inbox
    with rt.engine.connect() as c:
        last = c.execute(select(inbox).order_by(inbox.c.received_at.desc())).first()
    with rt.engine.begin() as c:
        by_address = parties.ask(
            c, rt, seat.id, channel="whatsapp", endpoint=ENDPOINT,
            message=lambda b: Outbound.template_message(
                "duo_invite", "en", buttons=(b["accept"], b["decline"])
            ),
        )  # fmt: skip
        inside = parties.ask_in(
            c, rt, seat.id, channel="whatsapp", conversation_id=last.conversation_id,
            identity_id=last.sender_identity_id, from_number=last.to, to=last.sender,
            message=lambda b: Outbound.text_message(f"join? {b['accept']}"),
        )  # fmt: skip
    assert by_address.id != inside.id
    await rt.send_once()
    o = rt.t.outbox
    with rt.engine.connect() as c:
        row = c.execute(select(o).where(o.c.turn_id == inside.turn_id)).one()
    token_button = row.message["text"].split(" ", 1)[1]
    pressed = replace(msg("g2", "Accept", T0, sender=GUEST_PHONE), kind="interactive",
                      reply_to=row.channel_message_id)  # fmt: skip
    reply = {"type": "button_reply", "button_reply": {"id": token_button, "title": "Accept"}}
    pressed = replace(pressed, payload={"id": "g2", "interactive": reply})
    receive(rt, [pressed])
    e = rt.t.expectations
    with rt.engine.connect() as c:
        assert parties.seat(c, seat.id).state == pt.SeatState.ACCEPTED
        states = dict(c.execute(select(e.c.id, e.c.state).where(e.c.kind == pt.KIND)).all())
    assert states == {by_address.id: "cancelled", inside.id: "answered"}


def test_a_seat_taken_by_another_door_cancels_the_unsent_ask_and_proposes_the_address(world):
    """Asked at a number nobody owns yet, taken by code from another account: the ask still
    waiting is not sent, and the number may be theirs (proposed), never linked on that alone."""
    rt, parties, _ = world
    host, guest = actor(rt), actor(rt)
    _, seat = made(rt, parties, host)
    with rt.engine.begin() as c:
        asked = parties.ask(
            c, rt, seat.id, channel="whatsapp", endpoint=ENDPOINT,
            message=lambda b: Outbound.template_message(
                "duo_invite", "en", buttons=(b["accept"], b["decline"])
            ),
        )  # fmt: skip
        parties.redeem(c, seat.code, guest)
    o, cl, i = rt.t.outbox, rt.t.claims, rt.t.identities
    with rt.engine.connect() as c:
        assert c.scalar(select(o.c.status).where(o.c.ref == f"expect:{asked.id}")) == "cancelled"
        claims = c.execute(
            select(cl.c.actor_id, cl.c.state, cl.c.basis)
            .join(i, i.c.id == cl.c.identity_id)
            .where(i.c.value == GUEST_PHONE)
        ).all()
    assert [tuple(r) for r in claims] == [(guest, "proposed", "invitation")]


def test_an_organizer_makes_a_party_without_a_seat_and_assigns_people_into_it(world):
    """Someone who arranges it (an organizer) takes no seat; people put in by them are in at
    once (door assigned), the ceiling and one seat per subject still hold."""
    rt, parties, duo = world
    organizer, a, b, c_ = actor(rt), actor(rt), actor(rt), actor(rt)
    with rt.engine.begin() as c:
        party, seat = parties.create(c, "demo.duo", subject="match:2", by=organizer, seated=False)
        assert seat is None and parties.seats(c, party.id) == []
        first = parties.assign(c, party.id, a, by=organizer)
        second = parties.assign(c, party.id, b, by=organizer)
        with pytest.raises(pt.SeatError) as full:
            parties.assign(c, party.id, c_, by=organizer)
        other, _ = parties.create(c, "demo.duo", subject="match:2", by=organizer, seated=False)
        with pytest.raises(pt.SeatError) as twice:
            parties.assign(c, other.id, a, by=organizer)
    assert first.seat.door == pt.Door.ASSIGNED and first.seat.invited_by == organizer
    assert second.party.state == pt.PartyState.CONFIRMED
    assert full.value.reason == "full" and twice.value.reason == "already_in"
    assert [h[2] for h in duo.heard] == ["assigned", "assigned"]
