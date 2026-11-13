"""Telegram adapter: request shapes against a fake Bot API (httpx.MockTransport), and updates
parsed into Inbound. Tokens and ids are invented."""

import asyncio
import json

import httpx
import pytest

from meydan.channels.base import Address, Outbound, SendError
from meydan.channels.telegram import Telegram, bot_id, parse_messages, poll
from meydan.channels.whatsapp import reply_payload

TOKEN = "111:secret-a"


def api(answers: dict[str, object] | None = None, status: int = 200):
    """A fake Bot API: records (path, body) and answers `ok` with a canned result per method."""
    calls: list[tuple[str, dict]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append((request.url.path, json.loads(request.content)))
        if status != 200:
            body = {
                "ok": False,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 7},
            }
            return httpx.Response(status, json=body)
        result = (answers or {}).get(method, {"message_id": 42})
        return httpx.Response(200, json={"ok": True, "result": result})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), calls


def render(message: Outbound):
    return f"{message.params['client']} soruyor", ("Evet", "Hayır")


async def test_text_goes_out_from_the_bot_written_to_and_returns_a_linkable_id():
    client, calls = api()
    sent = await Telegram([TOKEN], client=client).send("111", "900", Outbound.text_message("selam"))
    assert calls == [("/bot111:secret-a/sendMessage", {"chat_id": 900, "text": "selam"})]
    assert sent == "111:900:42"


async def test_a_template_becomes_a_text_with_buttons_that_carry_the_payloads():
    client, calls = api()
    ask = Outbound.template_message(
        "booking_confirm", "tr", buttons=("expect:tok:yes", "expect:tok:no"), client="Ada"
    )
    await Telegram([TOKEN], render=render, client=client).send("111", "900", ask)
    [(_, body)] = calls
    assert body["text"] == "Ada soruyor"
    assert body["reply_markup"] == {
        "inline_keyboard": [
            [
                {"text": "Evet", "callback_data": "expect:tok:yes"},
                {"text": "Hayır", "callback_data": "expect:tok:no"},
            ]
        ]
    }


async def test_a_rate_limit_is_retryable_with_the_wait_telegram_names():
    client, _ = api(status=429)
    with pytest.raises(SendError) as failed:
        await Telegram([TOKEN], client=client).send("111", "900", Outbound.text_message("x"))
    assert failed.value.retryable and failed.value.min_wait_s == 7
    assert "secret" not in str(failed.value)


async def test_an_unknown_bot_or_a_bad_token_fails_loudly():
    client, _ = api()
    with pytest.raises(SendError, match="no token for bot 222"):
        await Telegram([TOKEN], client=client).send("222", "900", Outbound.text_message("x"))
    with pytest.raises(ValueError, match="not a bot token"):
        bot_id("no-colon")


MESSAGE = {
    "update_id": 5,
    "message": {
        "message_id": 7, "date": 1793000000, "text": "merhaba",
        "from": {"id": 900, "first_name": "Ada", "last_name": "Kaya", "username": "ada"},
        "chat": {"id": 900, "type": "private"},
    },
}  # fmt: skip
PRESS = {
    "update_id": 6,
    "callback_query": {
        "id": "cb1", "data": "expect:tok:yes", "from": {"id": 900, "first_name": "Ada"},
        "message": {"message_id": 42, "chat": {"id": 900, "type": "private"}},
    },
}  # fmt: skip
GROUP = {
    "update_id": 7,
    "message": {
        "message_id": 8, "date": 1793000001, "text": "hi", "from": {"id": 901},
        "chat": {"id": -500, "type": "group"},
    },
}  # fmt: skip


async def test_updates_become_inbound_messages_and_presses_link_to_what_asked():
    said, pressed = parse_messages("111", [MESSAGE, PRESS, GROUP])  # the group message is skipped
    assert (said.channel, said.to, said.sender) == (
        "telegram", Address("tg-bot", "111"), Address("tg", "900"),
    )  # fmt: skip
    assert (said.message_id, said.text, said.sender_name) == ("111:900:7", "merhaba", "Ada Kaya")
    assert said.attrs == {"username": "ada"} and said.sent_at.tzinfo is not None
    assert pressed.kind == "button" and reply_payload(pressed) == "expect:tok:yes"
    assert pressed.reply_to == "111:900:42"  # the id `send` returned for the question


async def test_polling_stores_before_it_moves_on_and_acknowledges_presses():
    stop = asyncio.Event()
    client, calls = api({"getUpdates": [MESSAGE, PRESS], "answerCallbackQuery": True})
    channel = Telegram([TOKEN], client=client)
    stored: list[str] = []

    def receive(messages):
        stored.extend(m.message_id for m in messages)
        if len(stored) >= 4:  # two batches seen: enough
            stop.set()

    await asyncio.wait_for(poll(channel, "111", receive, stop), 5)
    polls = [body for path, body in calls if path.endswith("/getUpdates")]
    assert "offset" not in polls[0] and polls[1]["offset"] == 7  # past the last update stored
    assert stored[:2] == ["111:900:7", "111:cb:cb1"]
    acks = [body for path, body in calls if path.endswith("/answerCallbackQuery")]
    assert acks[0] == {"callback_query_id": "cb1"}


async def test_a_button_payload_over_telegrams_limit_is_refused_before_sending():
    client, calls = api()
    ask = Outbound.template_message("x", "tr", buttons=("p" * 65,), client="Ada")
    with pytest.raises(SendError, match="over 64 bytes"):
        await Telegram([TOKEN], render=lambda m: ("?", ("Evet",)), client=client).send(
            "111", "900", ask
        )
    assert calls == []
