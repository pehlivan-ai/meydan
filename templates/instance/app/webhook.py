"""The WhatsApp Cloud API webhook: verify, store, answer 200. Nothing slow happens before the
response; replies are made by the runtime's worker and sent by its sender, not here.

Cloud API retries undelivered webhooks for days, at least once and unordered: each message
enters meydan's inbox once (deduplicated on its id), with its raw payload.
"""

import hmac
import json
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from meydan.channels.whatsapp import (
    parse_messages,
    parse_statuses,
    parse_template_updates,
    verify_signature,
)
from meydan.runtime import Runtime

from app.config import Env


def routes(env: Env, runtime: Runtime) -> APIRouter:
    router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp"])

    @router.get("", response_class=PlainTextResponse)
    def verify(
        mode: Annotated[str, Query(alias="hub.mode")],
        token: Annotated[str, Query(alias="hub.verify_token")],
        challenge: Annotated[str, Query(alias="hub.challenge")],
    ) -> str:
        """Meta's subscription handshake: echo the challenge if our verify token matches."""
        expected = env.whatsapp_verify_token
        if mode != "subscribe" or not expected or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=403)
        return challenge

    @router.post("")
    async def receive(request: Request) -> dict[str, str]:
        body = await request.body()
        if not verify_signature(
            env.meta_app_secret, body, request.headers.get("X-Hub-Signature-256")
        ):
            raise HTTPException(status_code=401)
        payload = json.loads(body)
        with runtime.engine.begin() as conn:
            runtime.receive(
                conn,
                parse_messages(payload),
                parse_statuses(payload),
                parse_template_updates(payload),
            )
        return {"status": "ok"}

    return router
