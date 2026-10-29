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
