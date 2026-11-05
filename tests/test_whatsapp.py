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
        "latitude": 51.5,
        "longitude": -0.12,
        "name": "Court One",
        "address": "1 Main St",
    }
    assert pin.text == "Court One\n1 Main St"  # what transcripts show
    assert Outbound.from_payload(pin.to_payload()) == pin  # survives the outbox
    bare = Outbound.location_message(51.5, -0.12)
    await cloud(handler).send("222", "905550000001", bare)
    assert seen[1]["location"] == {"latitude": 51.5, "longitude": -0.12}
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
