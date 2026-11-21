"""Instagram adapter: request shapes against a fake Graph API (httpx.MockTransport), and webhook
payloads parsed into Inbound. Ids and tokens are invented."""

import json

import httpx
import pytest

from meydan.channels.base import Address, Choice, Outbound, SendError, reply_payload
from meydan.channels.instagram import (
    INSTAGRAM_LOGIN,
    TEXT_BYTES_MAX,
    Instagram,
    choice_problems,
    parse_messages,
    parse_statuses,
    split_text,
)
from meydan.rules import DEFAULT

OURS, THEM = "17840000000000001", "9000000000000002"


def graph(status: int = 200, error: dict | None = None):
    """A fake Graph API: records (path, headers, body) and answers like `/messages`."""
    calls: list[tuple[str, str, dict]] = []
    hosts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        calls.append(
            (request.url.path, request.headers["authorization"], json.loads(request.content))
        )
        if status != 200:
            return httpx.Response(status, json={"error": error or {}})
        return httpx.Response(200, json={"recipient_id": THEM, "message_id": "mid.out.1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client.hosts = hosts  # type: ignore[attr-defined]
    return client, calls


def webhook(*events: dict) -> dict:
    return {"object": "instagram", "entry": [{"id": OURS, "time": 1763800000000,
                                              "messaging": list(events)}]}  # fmt: skip


def dm(message: dict, ts: int = 1763800000123) -> dict:
    return {"sender": {"id": THEM}, "recipient": {"id": OURS}, "timestamp": ts,
            "message": message}  # fmt: skip


async def test_text_goes_out_from_the_account_written_to():
    client, calls = graph()
    sent = await Instagram("tok", client=client).send(OURS, THEM, Outbound.text_message("selam"))
    assert calls == [
        (f"/v27.0/{OURS}/messages", "Bearer tok",
         {"recipient": {"id": THEM}, "message": {"text": "selam"}}),
    ]  # fmt: skip
    assert sent == "mid.out.1"
    assert client.hosts == ["graph.facebook.com"]  # Unified Login, by default


async def test_the_older_instagram_login_setup_goes_to_its_own_host():
    client, _ = graph()
    ig = Instagram("tok", client=client, host=INSTAGRAM_LOGIN)
    await ig.send(OURS, THEM, Outbound.text_message("selam"))
    assert client.hosts == ["graph.instagram.com"]


async def test_choices_are_quick_replies_carrying_the_option_ids():
    client, calls = graph()
    ask = Outbound.choices_message(
        "Hangi gün?",
        [Choice("day:sat", "Cumartesi", "10:00 grubu"), Choice("day:sun", "Pazar")],
    )
    await Instagram("tok", client=client).send(OURS, THEM, ask)
    [(_, _, body)] = calls
    assert body["message"] == {
        "text": "Hangi gün?\n\n• Cumartesi: 10:00 grubu",  # descriptions go in the text
        "quick_replies": [
            {"content_type": "text", "title": "Cumartesi", "payload": "day:sat"},
            {"content_type": "text", "title": "Pazar", "payload": "day:sun"},
        ],
    }


async def test_choices_that_instagram_would_cut_are_refused_not_sent():
    ask = Outbound.choices_message("?", [Choice("a", "x" * 21)])
    assert choice_problems(ask) == ["a title over 20 characters"]
    client, calls = graph()
    with pytest.raises(SendError, match="20 characters") as e:
        await Instagram("tok", client=client).send(OURS, THEM, ask)
    assert not e.value.retryable and calls == []


async def test_a_location_and_a_contact_card_go_as_text():
    client, calls = graph()
    ig = Instagram("tok", client=client)
    pin = Outbound.location_message(36.8, 34.6, place="Salon", address="Cad 1")
    await ig.send(OURS, THEM, pin)
    await ig.send(OURS, THEM, Outbound.contact_message("Ada Yılmaz", "+905550000000"))
    texts = [body["message"]["text"] for _, _, body in calls]
    assert texts == [
        "Salon\nCad 1\nhttps://www.google.com/maps/search/?api=1&query=36.8,34.6",
        "Ada Yılmaz\n+905550000000",
    ]


@pytest.mark.parametrize(
    "message",
    [
        Outbound.template_message("reminder", "tr", name="Ada"),
        Outbound.image_message("sha256:abc"),
    ],
    ids=["template", "image"],
)
async def test_what_instagram_cannot_take_is_refused_loudly(message):
    client, calls = graph()
    with pytest.raises(SendError) as e:
        await Instagram("tok", client=client).send(OURS, THEM, message)
    assert not e.value.retryable and calls == []


async def test_rate_limits_are_retried_and_the_window_is_not():
    client, _ = graph(400, {"code": 613, "message": "Calls within one hour exceeded"})
    with pytest.raises(SendError) as e:
        await Instagram("tok", client=client).send(OURS, THEM, Outbound.text_message("a"))
    assert e.value.retryable and e.value.min_wait_s == 5.0

    window = {"code": 10, "error_subcode": 2534022, "message": "outside of allowed window"}
    client, _ = graph(400, window)
    with pytest.raises(SendError, match="10/2534022") as e:
        await Instagram("tok", client=client).send(OURS, THEM, Outbound.text_message("a"))
    assert not e.value.retryable


def test_a_dm_becomes_an_inbound_from_a_person_scoped_to_our_account():
    [m] = parse_messages(webhook(dm({"mid": "mid.in.1", "text": "merhaba"})))
    assert (m.channel, m.message_id, m.kind, m.text) == ("instagram", "mid.in.1", "text", "merhaba")
    assert m.to == Address("ig", OURS)
    assert m.sender == Address("igsid", THEM, OURS)
    assert m.sent_at.timestamp() == 1763800000.123  # milliseconds


def test_a_quick_reply_tap_is_a_button_press_with_its_payload():
    tap = dm({"mid": "mid.in.2", "text": "Pazar", "quick_reply": {"payload": "day:sun"}})
    [m] = parse_messages(webhook(tap))
    assert (m.kind, m.text, reply_payload(m)) == ("button", "Pazar", "day:sun")


def test_attachments_replies_and_unsupported_messages():
    photo = dm({"mid": "m3", "attachments": [{"type": "image", "payload": {"url": "https://x"}}]})
    quoted = dm({"mid": "m4", "text": "evet", "reply_to": {"mid": "mid.out.1"}})
    odd = dm({"mid": "m5", "is_unsupported": True})
    a, b, c = parse_messages(webhook(photo, quoted, odd))
    assert (a.kind, a.text) == ("image", "")
    assert (b.kind, b.reply_to) == ("text", "mid.out.1")
    assert c.kind == "unsupported"


def test_echoes_deletions_reactions_and_other_objects_are_skipped():
    echo = {"sender": {"id": OURS}, "recipient": {"id": THEM}, "timestamp": 1,
            "message": {"mid": "e", "text": "our reply", "is_echo": True}}  # fmt: skip
    deleted = dm({"mid": "d", "is_deleted": True})
    reaction = {"sender": {"id": THEM}, "recipient": {"id": OURS}, "timestamp": 1,
                "reaction": {"mid": "x", "action": "react", "emoji": "❤️"}}  # fmt: skip
    assert parse_messages(webhook(echo, deleted, reaction)) == []
    assert parse_messages({"object": "page", "entry": [{"messaging": [dm({"mid": "p"})]}]}) == []


def test_a_read_is_a_status_of_our_message():
    seen = {"sender": {"id": THEM}, "recipient": {"id": OURS}, "timestamp": 1763800000000,
            "read": {"mid": "mid.out.1"}}  # fmt: skip
    [s] = parse_statuses(webhook(seen))
    assert (s.channel, s.message_id, s.status) == ("instagram", "mid.out.1", "read")
    assert s.recipient == (Address("igsid", THEM, OURS),)


def test_the_rules_say_how_instagram_differs():
    rules = DEFAULT.of("instagram")
    assert rules.reply_window is not None and rules.text_bytes_max == TEXT_BYTES_MAX


def test_long_text_is_cut_where_a_line_ends_and_never_inside_a_character():
    lines = [f"{d}: Bale 10:00, Resim 14:00, Piyano 16:30 — öğretmen Ş." for d in range(40)]
    parts = split_text("\n".join(lines))
    assert len(parts) > 1 and all(len(p.encode()) <= TEXT_BYTES_MAX for p in parts)
    assert "\n".join(parts) == "\n".join(lines)  # cut between lines, nothing lost
    word = split_text("ş" * 600)  # no separator: cut inside the word, on a character boundary
    assert [len(p) for p in word] == [500, 100]


async def test_a_long_text_goes_as_several_messages_and_quick_replies_ride_on_the_last():
    client, calls = graph()
    body = "\n".join(f"satır {n} " + "x" * 60 for n in range(30))
    ask = Outbound.choices_message(body, [Choice("ok", "Tamam")])
    await Instagram("tok", client=client).send(OURS, THEM, ask)
    messages = [b["message"] for _, _, b in calls]
    assert len(messages) == 3
    assert all("quick_replies" not in m for m in messages[:-1])
    assert messages[-1]["quick_replies"] == [
        {"content_type": "text", "title": "Tamam", "payload": "ok"}
    ]


async def test_a_part_that_fails_after_others_went_is_not_tried_again():
    sent: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if len(sent) == 2:
            return httpx.Response(500, json={"error": {"code": 2, "message": "temporary"}})
        return httpx.Response(200, json={"recipient_id": THEM, "message_id": f"m{len(sent)}"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    long = "\n".join("y" * 90 for _ in range(30))
    with pytest.raises(SendError, match="sent 1 of 3 parts") as e:
        await Instagram("tok", client=client).send(OURS, THEM, Outbound.text_message(long))
    assert not e.value.retryable  # trying again would send the first part twice
