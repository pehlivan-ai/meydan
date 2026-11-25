"""A bridge's HTTP door (extra `api`): one route per connection, `POST /{connection}/webhooks`.

    app.include_router(bridge_routes(bridge), prefix="/connections")

The request is verified and stored in one short transaction, then answered (any 2xx tells a
Standard Webhooks sender it arrived; a repeat gets 200 as well). Handling happens in the worker
(`Bridge.run`), so a slow handler never makes the sender wait or retry.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from meydan.bridge import Bridge, RejectedError

MAX_BODY = 256 * 1024


class Ack(BaseModel):
    received: bool = True
    duplicate: bool = False


def routes(bridge: Bridge, *, max_body: int = MAX_BODY) -> APIRouter:
    router = APIRouter(tags=["bridges"])

    @router.post("/{connection}/webhooks", response_model=Ack)
    async def receive(connection: str, request: Request) -> Ack:
        body = await request.body()
        if len(body) > max_body:
            raise HTTPException(413, "body too large")
        head = dict(request.headers)

        def store():
            with bridge.engine.begin() as conn:
                return bridge.receive(conn, connection, head, body)

        try:
            got = await run_in_threadpool(store)
        except RejectedError as e:
            raise HTTPException(e.status, e.reason) from None
        return Ack(duplicate=got.duplicate)

    return router


__all__ = ["MAX_BODY", "Ack", "routes"]
