"""Notifications' HTTP surface (extras `notify` + `api`): a device says yes or no.

    app.include_router(notify_routes(auth, runtime.t, engine, keys=keys), prefix="/notify")

`GET /key` gives the page the public key to subscribe with. `POST /subscriptions` takes
what the browser's `PushSubscription.toJSON()` gives; `DELETE /subscriptions` ends one.
`GET /kinds` lists the kinds the app sends and `POST /test` sends one kind's example to the
caller's own devices. These need the `notify:subscribe` scope. Whose device it is: a person's
own session, or whom a service acts for (`X-On-Behalf-Of`), as with the operator's read marks.
Who may subscribe at all is the app's call (the scope, and a service that only forwards its own
staff).

`POST /notifications` is for another system of the same organisation sending through this one
pipe (`notify:send`): only kinds the app declared, links inside the app, to every subscribed
device or the subscribers it names.
"""

from collections.abc import Sequence
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
SEND = "notify:send"


class KindOut(BaseModel):
    name: str
    label: str
    when: str


class TestRequest(BaseModel):
    kind: str = Field(max_length=64)


class Tested(BaseModel):
    devices: int  # 0: this person has no device subscribed yet


class Notification(BaseModel):
    kind: str = Field(max_length=64)
    title: str = Field(min_length=1, max_length=notify.TITLE_MAX)
    body: str = Field(max_length=notify.BODY_MAX)
    url: str | None = Field(default=None, max_length=500, pattern=r"^/")  # in the app, never away
    tag: str | None = Field(default=None, max_length=200)
    to: list[str] | None = Field(default=None, max_length=200)


class Queued(BaseModel):
    devices: int


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


def routes(
    auth: Auth,
    t: RuntimeTables,
    engine: Engine,
    *,
    keys: notify.Keys | None,
    kinds: Sequence[notify.Kind] = (),
) -> APIRouter:
    router = APIRouter(tags=["notify"])
    member = Annotated[Principal, Depends(auth.require(SUBSCRIBE))]
    sender = Annotated[Principal, Depends(auth.require(SEND))]
    by_name = {k.name: k for k in kinds}

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

    @router.get("/kinds")
    async def list_kinds(person: member) -> list[KindOut]:
        return [KindOut(name=k.name, label=k.label, when=k.when) for k in kinds]

    @router.post("/test")
    async def send_test(person: member, req: TestRequest) -> Tested:
        kind = by_name.get(req.kind)
        if kind is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such kind")
        n = await run_in_threadpool(tx, notify.test, t, kind, whose(person))
        return Tested(devices=n)

    @router.post("/notifications")
    async def send(person: sender, n: Notification) -> Queued:
        if n.kind not in by_name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "undeclared kind")
        devices = await run_in_threadpool(
            tx, notify.enqueue, t,
            kind=n.kind, title=n.title, body=n.body, url=n.url, tag=n.tag, to=n.to,
        )  # fmt: skip
        return Queued(devices=devices)

    @router.delete("/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
    async def unsubscribe(person: member, sub: Ended) -> None:
        await run_in_threadpool(tx, notify.unsubscribe, t, whose(person), sub.endpoint)

    return router
