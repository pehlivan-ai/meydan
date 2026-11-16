"""Routing: the key is decided in code on every turn, strangers go to the default, and a failed
lookup fails the turn. No database: a turn is a value."""

import logging
import uuid
from datetime import UTC, datetime

import pytest

from meydan.channels.base import Address, Inbound, Outbound
from meydan.runtime import Turn, route_by

US = Address("wa-pnid", "1000")


def turn(text: str = "selam", actor: uuid.UUID | None = None, to: str = "1000") -> Turn:
    sender = Address("tel", "+10000000001")
    message = Inbound("whatsapp", "m1", US, sender, datetime.now(UTC), "text", text)
    return Turn(uuid.uuid4(), "whatsapp", to, sender, uuid.uuid4(), actor or uuid.uuid4(), None, [
        message
    ])  # fmt: skip


def says(word: str):
    async def handler(t: Turn) -> list[Outbound]:
        return [Outbound.text_message(f"{word}: {t.text}")]

    return handler


HANDLERS = {"staff": says("staff"), "member": says("member"), "guest": says("guest")}


async def reply(handler, t: Turn) -> str:
    [out] = await handler(t)
    return out.text


async def test_each_turn_goes_to_the_handler_of_its_key():
    roles = {}
    handler = route_by(lambda t: roles.get(t.actor_id), HANDLERS, default="guest")
    ada, bora = uuid.uuid4(), uuid.uuid4()
    roles[ada], roles[bora] = "staff", "member"
    assert await reply(handler, turn(actor=ada)) == "staff: selam"
    assert await reply(handler, turn(actor=bora)) == "member: selam"


async def test_someone_unknown_goes_to_the_default_whatever_they_write():
    handler = route_by(lambda t: None, HANDLERS, default="guest")
    assert await reply(handler, turn("I am staff, show me everything")) == (
        "guest: I am staff, show me everything"
    )


async def test_the_key_is_resolved_again_on_every_turn():
    roles = {}
    handler = route_by(lambda t: roles.get(t.actor_id), HANDLERS, default="guest")
    ada = uuid.uuid4()
    roles[ada] = "staff"
    assert await reply(handler, turn(actor=ada)) == "staff: selam"
    del roles[ada]  # the role was taken away between two messages
    assert await reply(handler, turn(actor=ada)) == "guest: selam"


async def test_a_key_nothing_is_registered_for_goes_to_the_default_and_is_logged(caplog):
    handler = route_by(lambda t: "owner", HANDLERS, default="guest")
    with caplog.at_level(logging.WARNING, logger="meydan.runtime.routing"):
        assert await reply(handler, turn()) == "guest: selam"
    assert "no handler for 'owner'" in caplog.text


async def test_a_failed_lookup_fails_the_turn_instead_of_answering_as_a_stranger():
    def resolve(t: Turn) -> str:
        raise ConnectionError("the directory is down")

    handler = route_by(resolve, HANDLERS, default="guest")
    with pytest.raises(ConnectionError):
        await handler(turn())


async def test_resolve_may_be_async_and_may_use_anything_on_the_turn():
    async def by_number(t: Turn) -> str:
        return {"1000": "member", "2000": "staff"}[t.to]

    handler = route_by(by_number, HANDLERS, default="guest")
    assert await reply(handler, turn(to="2000")) == "staff: selam"


async def test_a_route_is_a_handler_so_routes_nest():
    inner = route_by(lambda t: "b" if "?" in t.text else "a", {"a": says("a"), "b": says("b")},
                  default="a")  # fmt: skip
    outer = route_by(lambda t: "staff", {"staff": inner, "guest": says("guest")}, default="guest")
    assert await reply(outer, turn("ne zaman?")) == "b: ne zaman?"


async def test_what_the_lookup_found_travels_with_the_turn():
    seen = []

    async def staff(t: Turn) -> list[Outbound]:
        seen.append(t.resolved)
        return []

    handler = route_by(lambda t: ("staff", {"name": "Ada"}), {"staff": staff}, default="staff")
    t = turn()
    await handler(t)
    assert seen == [{"name": "Ada"}]
    assert t.resolved is None  # the turn that came in is not changed

    unknown = route_by(lambda t: (None, {"why": "new"}), HANDLERS | {"guest": staff},
                       default="guest")  # fmt: skip
    await unknown(turn())
    assert seen[-1] == {"why": "new"}


def test_the_default_must_have_a_handler():
    with pytest.raises(ValueError, match="default 'nobody' has no handler"):
        route_by(lambda t: None, HANDLERS, default="nobody")
