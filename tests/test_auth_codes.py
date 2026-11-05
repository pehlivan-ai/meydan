"""Sign in by a code we send (meydan.auth: start_code_sign_in, send_code, verify_code, reject),
end to end through the FastAPI routes and the runtime. Fake numbers and ids only."""

from dataclasses import replace
from datetime import timedelta
from typing import Annotated

import pytest
from conftest import T0, FakeChannel, msg, receive, runtime
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update

from meydan.auth import AuthConfig, Principal, grant, send_code, sign_in_interceptor
from meydan.auth.web import Auth
from meydan.channels.base import Address, Outbound

ADMIN, STRANGER = "+15550001001", "+15550001002"
OURS, OTHER_NUMBER = "222", "333"  # two of our numbers (phone_number_ids)


class CodeChannel(FakeChannel):
    async def send(self, from_number, to, message):
        self.sent.append((from_number, to, message.otp or message.text))
        return f"wamid.{len(self.sent)}"


async def echo(turn):
    return [Outbound.text_message("ok")]


@pytest.fixture
def setup(db):
    engine, t = db
    rt = runtime(db, echo, CodeChannel())
    rejected = []
    rt.intercept = [
        sign_in_interceptor(
            t, on_reject=lambda c, sid, routed: rejected.append(sid), rejected_reply="Cancelled."
        )
    ]
    # The admin talks to us on our second number.
    receive(rt, [msg("w0", "hello", T0, sender=ADMIN, to=OTHER_NUMBER)])
    receive(rt, [msg("s0", "hi", T0, sender=STRANGER)])
    with engine.begin() as c:
        admin = c.scalar(
            select(t.claims.c.actor_id)
            .join(t.identities, t.identities.c.id == t.claims.c.identity_id)
            .where(t.identities.c.value == ADMIN)
        )
        grant(c, t, admin, "ops")
        c.execute(update(t.inbox).values(processed_at=text("now()")))

    def deliver(conn, code):
        send_code(conn, rt, code, template="signin_code", language="tr", channel="whatsapp",
                  default_from=Address("wa-pnid", OURS))  # fmt: skip

    auth = Auth(
        engine, t, AuthConfig(audience="ops"), secure_cookies=False,
        parse_address=lambda s: Address("tel", s.replace(" ", "")) if s.startswith("+") else None,
        send_code=deliver,
    )  # fmt: skip
    app = FastAPI()
    app.include_router(auth.routes(), prefix="/auth")

    signed_in = Annotated[Principal, Depends(auth.signed_in)]

    @app.get("/me")
    def me(person: signed_in) -> dict:
        return {"actor": str(person.actor_id)}

    return rt, TestClient(app), admin, rejected


def outbox(rt):
    o = rt.t.outbox
    with rt.engine.connect() as c:
        return c.execute(select(o).order_by(o.c.created_at)).all()


async def test_a_code_goes_where_they_talk_and_signs_the_browser_in(setup):
    rt, client, admin, _ = setup
    assert client.post("/auth/sign-in/code", json={"address": "+1 555 000 1001"}).json() == {
        "expires_in_s": 300
    }
    [row] = outbox(rt)
    assert row.message["template"] == "signin_code" and row.ref.startswith("signin:")
    assert row.from_number == OTHER_NUMBER  # the number they talk to us on
    code = row.message["otp"]
    assert len(code) == 6 and code.isdigit()

    await rt.send_once()
    assert rt.channel.sent[-1] == (OTHER_NUMBER, ADMIN, code)
    assert outbox(rt)[0].message["otp"] == "******"  # not kept once it left

    wrong = "000000" if code != "000000" else "111111"
    assert client.post("/auth/sign-in/verify", json={"code": wrong}).json() == {"status": "wrong"}
    assert client.get("/me").status_code == 401
    ok = client.post("/auth/sign-in/verify", json={"code": f"{code[:3]} {code[3:]}"})
    assert ok.json() == {"status": "signed_in"}
    assert client.get("/me").json() == {"actor": str(admin)}
    again = client.post("/auth/sign-in/verify", json={"code": code})
    assert again.json() == {"status": "expired"}  # one session per code


async def test_same_answer_for_strangers_and_unknown_numbers(setup):
    rt, client, _, _ = setup
    for address in ("+15550001002", "+15550009999"):  # no scope; never wrote
        r = client.post("/auth/sign-in/code", json={"address": address})
        assert r.status_code == 200 and r.json() == {"expires_in_s": 300}
        assert client.post("/auth/sign-in/verify", json={"code": "123456"}).json() == {
            "status": "wrong"
        }
    assert outbox(rt) == []
    assert client.post("/auth/sign-in/code", json={"address": "not a number"}).status_code == 422


async def test_tries_and_sends_are_limited(setup):
    rt, client, _, _ = setup
    for _ in range(4):
        client.post("/auth/sign-in/code", json={"address": ADMIN})
    assert len(outbox(rt)) == 3  # sends_per_window
    code = outbox(rt)[-1].message["otp"]
    client.post("/auth/sign-in/code", json={"address": ADMIN})  # this browser: the 5th, unsent
    statuses = [
        client.post("/auth/sign-in/verify", json={"code": code}).json()["status"] for _ in range(5)
    ]
    assert statuses == ["wrong", "wrong", "wrong", "wrong", "expired"]


async def test_a_sent_code_typed_into_the_chat_does_nothing(setup):
    rt, client, _, _ = setup
    client.post("/auth/sign-in/code", json={"address": ADMIN})
    code = outbox(rt)[0].message["otp"]
    receive(rt, [msg("w1", code, T0 + timedelta(seconds=1), sender=ADMIN, to=OTHER_NUMBER)])
    assert await rt.work_once() == 1  # an ordinary message: sent codes are typed in the browser
    assert client.post("/auth/sign-in/verify", json={"code": code}).json() == {
        "status": "signed_in"
    }


async def test_i_didnt_request_a_code_cancels_the_sign_in(setup):
    rt, client, _, rejected = setup
    client.post("/auth/sign-in/code", json={"address": ADMIN})
    await rt.send_once()
    row = outbox(rt)[0]
    press = replace(
        msg("w2", "I didn't request a code", T0, sender=ADMIN, to=OTHER_NUMBER),
        kind="button",
        reply_to=row.channel_message_id,
        payload={"id": "w2", "button": {"payload": "DID_NOT_REQUEST_CODE", "text": "..."}},
    )
    receive(rt, [press])
    await rt.send_once()
    assert rt.channel.sent[-1][2] == "Cancelled." and len(rejected) == 1
    code_was = "x"  # the code itself is gone from the outbox; any code now ends it
    assert client.post("/auth/sign-in/verify", json={"code": code_was}).json() == {
        "status": "expired"
    }


async def test_whatsapp_sends_the_code_in_the_body_and_the_copy_button():
    import json

    import httpx

    from meydan.channels.whatsapp import WhatsAppCloud

    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})

    cloud = WhatsAppCloud("token", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await cloud.send("222", "+15550001001", Outbound.code_message("signin_code", "tr", "123456"))
    template = seen[0]["template"]
    assert template["name"] == "signin_code" and template["language"] == {"code": "tr"}
    assert template["components"] == [
        {"type": "body", "parameters": [{"type": "text", "text": "123456"}]},
        {"type": "button", "sub_type": "url", "index": "0",
         "parameters": [{"type": "text", "text": "123456"}]},
    ]  # fmt: skip
