"""Sign in by sending a message, sessions and scopes, end to end (runtime + FastAPI)."""

from datetime import timedelta
from typing import Annotated

import pytest
from conftest import T0, FakeChannel, msg, receive, runtime
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update

from meydan.auth import AuthConfig, Principal, grant, sign_in_interceptor
from meydan.auth.web import Auth
from meydan.channels.base import Outbound

ADMIN, STRANGER = "+15550001001", "+15550001002"  # fake numbers: this is a public repository


async def echo(turn):
    return [Outbound.text_message(f"got: {turn.text}")]


@pytest.fixture
def setup(db):
    engine, t = db
    rt = runtime(db, echo, FakeChannel())
    rt.intercept = [sign_in_interceptor(t, confirmed_reply="Signed in.")]
    receive(rt, [msg("w0", "hello", T0, sender=ADMIN)])  # the admin is a known person
    with engine.begin() as c:
        admin = c.scalar(
            select(t.claims.c.actor_id)
            .join(t.identities, t.identities.c.id == t.claims.c.identity_id)
            .where(t.identities.c.value == ADMIN)
        )
        grant(c, t, admin, "ops")
        c.execute(update(t.inbox).values(processed_at=text("now()")))  # "hello" was answered
    auth = Auth(
        engine,
        t,
        AuthConfig(audience="ops", code_prefix="OPS"),
        secure_cookies=False,
        sign_in_link=lambda code: f"https://wa.me/15550000000?text={code}",
    )
    app = FastAPI()
    app.include_router(auth.routes(), prefix="/auth")
    signed_in = Annotated[Principal, Depends(auth.signed_in)]

    @app.get("/me")
    def me(person: signed_in) -> dict:
        return {"actor": str(person.actor_id), "scopes": sorted(person.scopes)}

    @app.get("/costs", dependencies=[Depends(auth.require("costs:read"))])
    def costs() -> dict:
        return {"ok": True}

    return rt, auth, TestClient(app), admin


def sign_in(rt, client, sender=ADMIN, mid="w1"):
    started = client.post("/auth/sign-in").json()
    receive(rt, [msg(mid, started["code"], T0, sender=sender)])
    return started


async def test_sign_in_by_sending_the_code(setup):
    rt, auth, client, admin = setup
    started = client.post("/auth/sign-in").json()
    assert started["code"].startswith("OPS-") and started["link"].endswith(started["code"])
    assert client.get("/auth/sign-in").json() == {"status": "pending"}
    assert client.get("/me").status_code == 401

    receive(rt, [msg("w1", f"  {started['code'].lower()} ", T0, sender=ADMIN)])  # case, spaces
    assert await rt.work_once() == 0  # the code never reaches the agent
    await rt.send_once()
    assert rt.channel.sent[-1][2] == "Signed in."

    assert client.get("/auth/sign-in").json() == {"status": "signed_in"}
    me = client.get("/me").json()
    assert me == {"actor": str(admin), "scopes": ["ops"]}
    assert client.get("/auth/sign-in").json() == {"status": "expired"}  # one session per code


async def test_only_people_with_the_scope_can_confirm(setup):
    rt, auth, client, _ = setup
    sign_in(rt, client, sender=STRANGER)
    assert await rt.work_once() == 1  # not intercepted: an ordinary message to the agent
    assert client.get("/auth/sign-in").json() == {"status": "pending"}


async def test_expired_codes_do_nothing(setup):
    rt, auth, client, _ = setup
    started = client.post("/auth/sign-in").json()
    with rt.engine.begin() as c:
        c.execute(update(rt.t.sign_ins).values(expires_at=text("now() - interval '1 s'")))
    receive(rt, [msg("w1", started["code"], T0 + timedelta(seconds=1), sender=ADMIN)])
    assert client.get("/auth/sign-in").json() == {"status": "expired"}


async def test_scopes_csrf_bearer_and_sign_out(setup):
    rt, auth, client, admin = setup
    sign_in(rt, client)
    client.get("/auth/sign-in")
    assert client.get("/costs").status_code == 403
    with rt.engine.begin() as c:
        grant(c, rt.t, admin, "costs:read")
    assert client.get("/costs").status_code == 200

    token = client.cookies.get("meydan_session")
    other = TestClient(client.app)  # no cookies: a mobile app or a service
    assert other.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    assert client.post("/auth/sign-out").status_code == 403  # cookie + no Origin: refused
    evil, own = {"Origin": "https://evil.example"}, {"Origin": "http://testserver"}
    assert client.post("/auth/sign-out", headers=evil).status_code == 403
    assert client.post("/auth/sign-out", headers=own).status_code == 204
    assert other.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


async def test_secrets_are_stored_only_as_hashes(setup):
    rt, auth, client, _ = setup
    started = sign_in(rt, client)
    client.get("/auth/sign-in")
    token = client.cookies.get("meydan_session")
    with rt.engine.connect() as c:
        rows = c.execute(select(rt.t.sign_ins)).all() + c.execute(select(rt.t.sessions)).all()
    dump = str(rows)
    assert started["code"] not in dump and token not in dump
