import hashlib
import hmac
import json

import httpx
import pytest

from meydan.channels.base import Outbound, SendError
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
    assert {m.to for m in msgs} == {"222"}
    assert msgs[0].sender_name == "Ayşe"
    assert msgs[0].conversation_key == "whatsapp:222:905550000001"
    assert msgs[0].sent_at.tzinfo is not None


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
    assert seen[0][1]["text"] == {"body": "hi"}
    t = seen[1][1]["template"]
    assert t["name"] == "tournament_reminder" and t["language"] == {"code": "tr"}
    assert t["components"][0]["parameters"][0] == {
        "type": "text",
        "parameter_name": "club",
        "text": "Moda",
    }


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
