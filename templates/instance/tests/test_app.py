"""The plumbing, end to end: Meta's handshake, the signature check, and one signed message
through the webhook and the running loops to your reply. Fake channel, real database
(`DATABASE_URL`, migrated). Message ids are new on every run, so nothing needs to be cleaned."""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient
from meydan.runtime import RuntimeConfig

from app.config import Env
from app.main import create_app

SECRET, PATH = "app-secret", "/webhooks/whatsapp"


class Fake:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, from_number, to, message) -> str:
        self.sent.append((to, message.text))
        return f"wamid.test.{time.time_ns()}"

    async def typing(self, from_number, message_id) -> None:
        pass


@pytest.fixture
def app():
    env = Env(meta_app_secret=SECRET, whatsapp_verify_token="handshake")
    fast = RuntimeConfig(
        debounce_s=0, max_wait_s=0, chars_per_second=1e9, presence="quiet", poll_s=0.05
    )
    app = create_app(env, channel=Fake(), config=fast)
    app.state.channel = app.state.runtime.channel
    return app


def message(text: str, sender: str) -> bytes:
    value = {
        "metadata": {"phone_number_id": "111"},
        "contacts": [{"wa_id": sender, "profile": {"name": "Ada"}}],
        "messages": [
            {"id": f"wamid.{time.time_ns()}", "from": sender, "timestamp": str(int(time.time())),
             "type": "text", "text": {"body": text}}
        ],
    }  # fmt: skip
    payload = {"object": "whatsapp_business_account",
               "entry": [{"changes": [{"field": "messages", "value": value}]}]}  # fmt: skip
    return json.dumps(payload).encode()


def signed(body: bytes, secret: str = SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


def test_the_handshake_echoes_the_challenge_only_for_our_token(app):
    hub = {"hub.mode": "subscribe", "hub.challenge": "12345"}
    with TestClient(app) as client:
        ok = client.get(PATH, params=hub | {"hub.verify_token": "handshake"})
        wrong = client.get(PATH, params=hub | {"hub.verify_token": "guess"})
    assert (ok.status_code, ok.text) == (200, "12345")
    assert wrong.status_code == 403


def test_a_webhook_that_meta_did_not_sign_is_refused(app):
    body = message("hello", "15550000001")
    with TestClient(app) as client:
        unsigned = client.post(PATH, content=body)
        forged = client.post(PATH, content=body, headers=signed(body, "another-secret"))
    assert (unsigned.status_code, forged.status_code) == (401, 401)
    assert app.state.channel.sent == []


def test_a_signed_message_is_answered_once_even_when_delivered_twice(app):
    sender = f"1555{time.time_ns() % 10**7:07d}"  # a new person each run
    body = message("hello", sender)
    with TestClient(app) as client:
        assert client.post(PATH, content=body, headers=signed(body)).status_code == 200
        assert client.post(PATH, content=body, headers=signed(body)).status_code == 200
        deadline = time.monotonic() + 5
        while not app.state.channel.sent and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)  # a second reply would have come by now
    assert app.state.channel.sent == [(f"+{sender}", "You wrote: hello")]
