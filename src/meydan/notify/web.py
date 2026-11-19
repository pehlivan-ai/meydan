"""Notifications' HTTP surface (extras `notify` + `api`): a device says yes or no.

    app.include_router(notify_routes(auth, runtime.t, engine, keys=keys), prefix="/notify")

`GET /key` gives the page the public key to subscribe with. `POST /subscriptions` takes what
the browser's `PushSubscription.toJSON()` gives; `DELETE /subscriptions` ends one. All need the
`notify:subscribe` scope. Whose device it is: a person's own session, or whom a service acts for
(`X-On-Behalf-Of`), as with the operator's read marks. Who may subscribe at all is the app's
call (the scope, and a service that only forwards its own staff).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import Engine

from meydan import notify
from meydan.auth.core import Principal
from meydan.auth.web import Auth
from meydan.runtime.tables import RuntimeTables

SUBSCRIBE = "notify:subscribe"


class Key(BaseModel):
    public_key: str | None  # None: no keys configured, nothing can be sent


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=20, max_length=200)
    auth: str = Field(min_length=8, max_length=64)


class Subscription(BaseModel):
    endpoint: str = Field(min_length=10, max_length=2000, pattern=r"^https://")
    keys: SubscriptionKeys


class Ended(BaseModel):
    endpoint: str = Field(min_length=10, max_length=2000)


def whose(person: Principal) -> str:
    """Whose device: whom a service acts for, else the person's own actor."""
    return person.on_behalf_of or str(person.actor_id)


def routes(auth: Auth, t: RuntimeTables, engine: Engine, *, keys: notify.Keys | None) -> APIRouter:
    router = APIRouter(tags=["notify"])
    member = Annotated[Principal, Depends(auth.require(SUBSCRIBE))]

    def tx(fn, *args, **kw):
        with engine.begin() as conn:
            return fn(conn, *args, **kw)

    @router.get("/key")
    async def key(person: member) -> Key:
        return Key(public_key=keys.public if keys else None)

    @router.post("/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
    async def subscribe(person: member, sub: Subscription) -> None:
        if keys is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "notifications are not set up")
        await run_in_threadpool(
            tx, notify.subscribe, t, whose(person),
            endpoint=sub.endpoint, p256dh=sub.keys.p256dh, auth=sub.keys.auth,
        )  # fmt: skip

    @router.delete("/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
    async def unsubscribe(person: member, sub: Ended) -> None:
        await run_in_threadpool(tx, notify.unsubscribe, t, whose(person), sub.endpoint)

    return router
