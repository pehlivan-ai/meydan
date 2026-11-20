import hashlib
import hmac
import json

import httpx
import pytest

from meydan.channels.base import Address, Outbound, SendError
from meydan.channels.whatsapp import (
    WhatsAppCloud,
    parse_messages,
    parse_statuses,
    reply_payload,
    verify_signature,
)


def webhook(messages=(), statuses=(), phone_number_id="111"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "contacts": [{"wa_id": "905550000001", "profile": {"name": "Ayşe"}}],
                            "messages": list(messages),
                            "statuses": list(statuses),
                        },
                    }
                ]
            }
        ],
    }


def test_signature():
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_signature("secret", body, sig)
    assert not verify_signature("secret", body + b" ", sig)
    assert not verify_signature("", body, sig)
    assert not verify_signature("secret", body, None)


def test_parses_text_button_and_interactive_routed_by_number():
    msgs = parse_messages(
        webhook(
            [
                {"id": "w1", "from": "905550000001", "timestamp": "1793000000", "type": "text",
                 "text": {"body": "selam"}},
                {"id": "w2", "from": "905550000001", "timestamp": "1793000001", "type": "button",
                 "button": {"text": "Onaylıyorum", "payload": "yes"}},
                {"id": "w3", "from": "905550000001", "timestamp": "1793000002",
                 "type": "interactive",
                 "interactive": {"type": "button_reply",
                                 "button_reply": {"id": "a", "title": "A"}}},
            ],
            phone_number_id="222",
        )
    )  # fmt: skip
    assert [m.text for m in msgs] == ["selam", "Onaylıyorum", "A"]
    assert {m.to for m in msgs} == {Address("wa-pnid", "222")}
    assert msgs[0].sender == Address("tel", "+905550000001")
    assert msgs[0].sender_name == "Ayşe" and msgs[0].also == ()
    assert msgs[0].sent_at.tzinfo is not None


def test_username_users_route_by_bsuid_and_messages_without_sender_are_skipped():
    payload = webhook(
        [
            {"id": "w1", "from": "905550000001", "from_user_id": "TR.111",
             "timestamp": "1793000000", "type": "text", "text": {"body": "numaralı"}},
            {"id": "w2", "from_user_id": "TR.222", "timestamp": "1793000001", "type": "text",
             "text": {"body": "sadece kullanıcı adı"}},
            {"id": "w3", "timestamp": "1793000002", "type": "text", "text": {"body": "kimsesiz"}},
        ],
        phone_number_id="222",
    )  # fmt: skip
    payload["entry"][0]["changes"][0]["value"]["contacts"].append(
        {"user_id": "TR.222", "profile": {"name": "Deniz", "username": "deniz"}}
    )
    msgs = parse_messages(payload, portfolio="p1")
    # Phone first, else the BSUID; together they are one sender (channel co-occurrence).
    assert [m.sender for m in msgs] == [
        Address("tel", "+905550000001"),
        Address("wa-bsuid", "TR.222", "p1"),
    ]
    assert msgs[0].also == (Address("wa-bsuid", "TR.111", "p1"),)
    assert msgs[1].sender_name == "Deniz" and msgs[1].attrs == {"username": "deniz"}


def test_parses_statuses_with_errors():
    sts = parse_statuses(
        webhook(
            statuses=[
                {"id": "w9", "status": "failed", "timestamp": "1793000005",
                 "errors": [{"code": 131047, "title": "Re-engagement message"}]},
            ]
        )
    )  # fmt: skip
    assert sts[0].status == "failed"
    assert sts[0].error == "131047: Re-engagement message"


def cloud(handler) -> WhatsAppCloud:
    return WhatsAppCloud("token", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_send_text_and_template_bodies():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})

    c = cloud(handler)
    assert await c.send("222", "905550000001", Outbound.text_message("hi")) == "wamid.X"
    await c.send(
        "222", "905550000001", Outbound.template_message("tournament_reminder", "tr", club="Moda")
    )
    assert seen[0][0].endswith("/222/messages")  # sent from the number they wrote to
    assert seen[0][1]["to"] == "905550000001" and "recipient" not in seen[0][1]
    assert seen[0][1]["text"] == {"body": "hi"}
    t = seen[1][1]["template"]
    assert t["name"] == "tournament_reminder" and t["language"] == {"code": "tr"}
    assert t["components"][0]["parameters"][0] == {
        "type": "text",
        "parameter_name": "club",
        "text": "Moda",
    }


async def test_template_buttons_carry_payloads_and_come_back():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.Z"}]})

    message = Outbound.template_message("invite", "tr", buttons=("accept:t1", "decline:t1"), n="A")
    await cloud(handler).send("222", "905550000001", message)
    components = seen[0]["template"]["components"]
    assert components[0]["type"] == "body"
    assert components[1:] == [
        {"type": "button", "sub_type": "quick_reply", "index": str(i),
         "parameters": [{"type": "payload", "payload": p}]}
        for i, p in enumerate(("accept:t1", "decline:t1"))
    ]  # fmt: skip
    assert Outbound.from_payload(message.to_payload()) == message  # survives the outbox

    pressed = parse_messages(
        {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "222"},
            "messages": [{"from": "905550000001", "id": "wamid.B", "timestamp": "1793300000",
                          "type": "button", "button": {"text": "Accept", "payload": "accept:t1"}}],
        }}]}]}
    )  # fmt: skip
    assert pressed[0].text == "Accept" and reply_payload(pressed[0]) == "accept:t1"


async def test_send_location_pin():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.L"}]})

    pin = Outbound.location_message(51.5, -0.12, place="Court One", address="1 Main St", ref="c:1")
    assert await cloud(handler).send("222", "905550000001", pin) == "wamid.L"
    assert seen[0]["type"] == "location"
    assert seen[0]["location"] == {
        "latitude": "51.5",
        "longitude": "-0.12",
        "name": "Court One",
        "address": "1 Main St",
    }
    assert pin.text == "Court One\n1 Main St"  # what transcripts show
    assert Outbound.from_payload(pin.to_payload()) == pin  # survives the outbox
    bare = Outbound.location_message(51.5, -0.12)
    await cloud(handler).send("222", "905550000001", bare)
    assert seen[1]["location"] == {"latitude": "51.5", "longitude": "-0.12"}
    with pytest.raises(ValueError):
        Outbound.location_message(91.0, -0.12)


def test_old_outbox_payloads_still_load():
    old = {"kind": "text", "text": "hi", "template": None, "language": None, "params": {},
           "buttons": [], "ref": None, "otp": None}  # fmt: skip
    assert Outbound.from_payload(old) == Outbound.text_message("hi")


async def test_send_to_bsuid_uses_recipient():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.Y"}]})

    await cloud(handler).send("222", "US.13491208655302741918", Outbound.text_message("hi"))
    assert seen[0]["recipient"] == "US.13491208655302741918" and "to" not in seen[0]


@pytest.mark.parametrize(
    ("status", "code", "retryable", "min_wait"),
    [
        (400, 131056, True, 4.0),  # pair rate limit
        (500, None, True, 0),
        (429, None, True, 0),
        (400, 131047, False, 0),  # outside 24h window: needs a template, retry won't help
        (401, 190, False, 0),  # bad token
    ],
)
async def test_error_classification(status, code, retryable, min_wait):
    def handler(request):
        return httpx.Response(status, json={"error": {"code": code, "message": "x"}})

    with pytest.raises(SendError) as e:
        await cloud(handler).send("222", "1", Outbound.text_message("x"))
    assert e.value.retryable is retryable
    assert e.value.min_wait_s == min_wait


async def test_bad_config_is_permanent_and_typing_never_raises():
    def bad_header(request):  # what httpx raises for a malformed header, with no network involved
        raise httpx.LocalProtocolError("Illegal header value b'Bearer '")

    c = cloud(bad_header)
    with pytest.raises(SendError) as e:
        await c.send("222", "1", Outbound.text_message("x"))
    assert e.value.retryable is False
    await c.typing("222", "wamid.1")  # best effort: must not raise


def test_statuses_name_the_recipient_by_phone_and_bsuid():
    sts = parse_statuses(
        webhook(
            statuses=[
                {"id": "w1", "status": "delivered", "timestamp": "1793000005",
                 "recipient_id": "905550000001", "recipient_user_id": "TR.111"},
                {"id": "w2", "status": "sent", "timestamp": "1793000006",
                 "recipient_id": "TR.111"},
            ]
        ),
        portfolio="p1",
    )  # fmt: skip
    phone, user = Address("tel", "+905550000001"), Address("wa-bsuid", "TR.111", "p1")
    assert sts[0].recipient == (phone, user)
    assert sts[1].recipient == (Address("wa-bsuid", "TR.111", "p1"),)


def test_shared_contacts_are_parsed_and_only_names_reach_the_text():
    from meydan.channels.whatsapp import shared_contacts

    [m] = parse_messages(
        webhook(
            messages=[
                {"from": "905550000001", "id": "w1", "timestamp": "1793000000", "type": "contacts",
                 "contacts": [
                     {"name": {"formatted_name": "Ece Test", "first_name": "Ece"},
                      "phones": [{"phone": "+90 555 000 00 02", "wa_id": "905550000002",
                                  "type": "CELL"},
                                 {"phone": "0212 000 00 00", "type": "WORK"}]},
                     {"name": {"first_name": "Ali"}, "phones": []},
                 ]},
            ]
        )
    )  # fmt: skip
    assert m.text == "Ece Test"  # no numbers in the text a model may see
    ece, ali = shared_contacts(m)
    assert ece.name == "Ece Test" and ali.name == "Ali" and ali.phones == ()
    assert ece.phones[0].address == Address("tel", "+905550000002")
    assert ece.phones[1].address is None and ece.phones[1].phone == "0212 000 00 00"


def test_a_button_press_says_which_message_it_answers():
    [m] = parse_messages(
        webhook(
            messages=[
                {"from": "905550000001", "id": "w1", "timestamp": "1793000000", "type": "button",
                 "context": {"from": "15550000000", "id": "wamid.OURS"},
                 "button": {"payload": "invite:accept:x", "text": "Kabul ediyorum"}},
            ]
        )
    )  # fmt: skip
    assert m.reply_to == "wamid.OURS"


async def test_send_choices_as_buttons_or_list():
    from meydan.channels.base import Choice
    from meydan.channels.whatsapp import choice_style

    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.C"}]})

    yes_no = Outbound.choices_message(
        "Join?", [Choice("y", "Yes"), Choice("n", "No")], label="Options", ref="q:1"
    )
    assert choice_style(yes_no) == "buttons"
    assert await cloud(handler).send("222", "905550000001", yes_no) == "wamid.C"
    assert seen[0]["type"] == "interactive"
    assert seen[0]["interactive"] == {
        "type": "button",
        "body": {"text": "Join?"},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": "y", "title": "Yes"}},
                {"type": "reply", "reply": {"id": "n", "title": "No"}},
            ]
        },
    }
    assert yes_no.text == "Join?\n• Yes\n• No"  # what transcripts show
    assert Outbound.from_payload(yes_no.to_payload()) == yes_no  # survives the outbox

    levels = [
        Choice(f"l{i}", f"Level {i}", "how you play" if i == 1 else None) for i in range(1, 8)
    ]
    many = Outbound.choices_message("Your level?", levels, label="Pick")
    assert choice_style(many) == "list"
    await cloud(handler).send("222", "905550000001", many)
    inter = seen[1]["interactive"]
    assert inter["type"] == "list"
    assert inter["action"]["button"] == "Pick"
    rows = inter["action"]["sections"][0]["rows"]
    assert rows[0] == {"id": "l1", "title": "Level 1", "description": "how you play"}
    assert rows[1] == {"id": "l2", "title": "Level 2"} and len(rows) == 7

    # A description alone makes it a list, so a label is needed; without one it never fits.
    no_label = Outbound.choices_message("Q?", [Choice("a", "A", "more")])
    with pytest.raises(ValueError, match="no list label"):
        choice_style(no_label)
    with pytest.raises(SendError) as e:
        await cloud(handler).send("222", "905550000001", no_label)
    assert not e.value.retryable

    with pytest.raises(ValueError):
        Outbound.choices_message("Q?", [Choice(str(i), str(i)) for i in range(11)])
    with pytest.raises(ValueError):
        Outbound.choices_message("Q?", [Choice("a", "Same"), Choice("b", "same")])
    with pytest.raises(ValueError):
        Outbound.choices_message(" ", [Choice("a", "A")])


def test_list_reply_payload_is_the_row_id():
    from meydan.channels.base import Inbound
    from meydan.channels.whatsapp import reply_payload

    def inbound(inter):
        return Inbound(
            "whatsapp", "wamid.R", None, None, None, "interactive", "",
            {"interactive": inter},
        )  # fmt: skip

    assert reply_payload(inbound({"type": "list_reply", "list_reply": {"id": "l4"}})) == "l4"
    assert reply_payload(inbound({"type": "button_reply", "button_reply": {"id": "y"}})) == "y"


async def test_send_a_contact_card_people_can_message_or_save():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.K"}]})

    card = Outbound.contact_message("Ada Kaya", "+905550000002", org="Club", title="Partnerships")
    assert await cloud(handler).send("222", "905550000001", card) == "wamid.K"
    assert seen[0]["type"] == "contacts"
    assert seen[0]["contacts"] == [
        {
            "name": {"formatted_name": "Ada Kaya", "first_name": "Ada", "last_name": "Kaya"},
            "phones": [{"phone": "+905550000002", "type": "WORK", "wa_id": "905550000002"}],
            "org": {"company": "Club", "title": "Partnerships"},
        }
    ]
    assert card.text == "Ada Kaya\nPartnerships\nClub\n+905550000002"  # what transcripts show
    assert Outbound.from_payload(card.to_payload()) == card
    bare = Outbound.contact_message("Ada", "+905550000002")
    await cloud(handler).send("222", "905550000001", bare)
    assert "org" not in seen[1]["contacts"][0] and "last_name" not in seen[1]["contacts"][0]["name"]
    for phone in ("05550000002", "+90 555", "+abc"):
        with pytest.raises(ValueError):
            Outbound.contact_message("Ada", phone)
    with pytest.raises(ValueError):
        Outbound.contact_message(" ", "+905550000002")


async def test_upload_a_file_then_send_the_image_by_id():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "4490709327384033"})
        return httpx.Response(200, json={"messages": [{"id": "wamid.I"}]})

    wa = cloud(handler)
    assert await wa.upload("222", b"\xff\xd8jpeg", "image/jpeg") == "4490709327384033"
    up = seen[0]
    assert up.url.path.endswith("/222/media")
    assert up.headers["content-type"].startswith("multipart/form-data")
    assert b'name="messaging_product"' in up.content and b"image/jpeg" in up.content

    from dataclasses import replace

    image = replace(Outbound.image_message("k" * 64, caption="Hi"), channel_media_id="4490")
    assert await wa.send("222", "905550000001", image) == "wamid.I"
    body = json.loads(seen[1].content)
    assert body["type"] == "image" and body["image"] == {"id": "4490", "caption": "Hi"}
    with pytest.raises(SendError):  # never uploaded: nothing to send
        await wa.send("222", "905550000001", Outbound.image_message("k" * 64))
    assert Outbound.from_payload(image.to_payload()) == image  # the id isn't part of the row
    assert "channel_media_id" not in image.to_payload()
