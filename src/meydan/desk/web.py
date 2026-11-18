"""The desk's HTTP surface (extra `api`): FastAPI routes over `meydan.desk`.

    app.include_router(desk_routes(auth, runtime, labels=...), prefix="/desk")

Reads need the operator surface and every conversation (`ops`, `conversations:read:all`); writes
(hold, release, messages) also need `desk:write`. Who is acting is the principal: a person's own
session, or a service key reading and writing for someone (`X-On-Behalf-Of`), whose id keeps their
own read marks and is recorded on what they do.

`labels` is the app's own words about the people in the list (a role, a registered name): a
function from actor ids to a dict per actor, shown as given. meydan knows nothing of them.
"""

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from meydan import desk
from meydan.auth.core import Principal
from meydan.auth.web import Auth
from meydan.observe import query
from meydan.observe.query import NEED_TO_KNOW, ConversationRow
from meydan.visibility import Visibility

READ = ("ops", "conversations:read:all")
WRITE = "desk:write"

Labels = Callable[[list[uuid.UUID]], dict[uuid.UUID, dict[str, Any]]]


class DeskRow(ConversationRow):
    unread: int = 0
    held_by: str | None = None
    held_until: datetime | None = None
    held: bool = False
    labels: dict[str, Any] = {}


class HoldRequest(BaseModel):
    minutes: int | None = Field(default=None, ge=1, le=7 * 24 * 60)  # None: until handed back


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4096)
    name: str | None = Field(
        default=None, max_length=255
    )  # who is writing, as their team calls them


class Sent(BaseModel):
    turn_id: uuid.UUID
    held_until: datetime | None


def reader(person: Principal) -> str:
    """Whose read marks: whom a service acts for, else the person's own actor."""
    return person.on_behalf_of or str(person.actor_id)


def routes(
    auth: Auth,
    runtime,
    *,
    labels: Labels | None = None,
    visibility: Visibility = NEED_TO_KNOW,
    hold_after_writing: timedelta | None = timedelta(minutes=30),
) -> APIRouter:
    router = APIRouter(tags=["desk"])
    reads = Annotated[Principal, Depends(auth.require(*READ))]
    writes = Annotated[Principal, Depends(auth.require(*READ, WRITE))]
    engine, t = runtime.engine, runtime.t

    def tx(fn, *args):
        with engine.begin() as conn:
            return fn(conn, *args)

    @router.get("/conversations")
    async def conversations(person: reads, limit: int = 100) -> list[DeskRow]:
        """The conversations, newest first, with this reader's unread count and any hold."""

        def read(conn):
            rows = query.conversations(conn, t, person, min(limit, 200), visibility=visibility)
            ids = [r.id for r in rows]
            unread = desk.unread(conn, t, reader(person), ids)
            held = desk.holds(conn, t, ids)
            return rows, unread, held

        rows, unread, held = await run_in_threadpool(tx, read)
        said = labels([r.actor_id for r in rows if r.actor_id]) if labels else {}
        out = []
        for r in rows:
            h = held.get(r.id)
            out.append(
                DeskRow(
                    **r.model_dump(),
                    unread=unread.get(r.id, 0),
                    held=h is not None,
                    held_by=h.held_by if h else None,
                    held_until=h.until if h else None,
                    labels=said.get(r.actor_id, {}) if r.actor_id else {},
                )
            )
        out.sort(key=lambda d: d.last_message_at or d.last_at or datetime.min, reverse=True)
        return out

    @router.post("/conversations/{conversation_id}/read", status_code=204)
    async def read(person: reads, conversation_id: uuid.UUID) -> None:
        await run_in_threadpool(tx, lambda c: desk.mark_read(c, t, conversation_id, reader(person)))

    @router.post("/conversations/{conversation_id}/hold")
    async def hold(person: writes, conversation_id: uuid.UUID, body: HoldRequest) -> desk.Hold:
        span = timedelta(minutes=body.minutes) if body.minutes else None
        return await run_in_threadpool(
            tx, lambda c: desk.hold(c, t, conversation_id, reader(person), span)
        )

    @router.post("/conversations/{conversation_id}/release", status_code=204)
    async def release(person: writes, conversation_id: uuid.UUID) -> None:
        await run_in_threadpool(tx, lambda c: desk.release(c, t, conversation_id))

    @router.post("/conversations/{conversation_id}/messages")
    async def message(person: writes, conversation_id: uuid.UUID, body: MessageRequest) -> Sent:
        def write(conn):
            by = (
                desk.staff_actor(conn, t, person.on_behalf_of, body.name)
                if person.on_behalf_of
                else person.actor_id
            )
            turn = desk.send(
                conn, runtime, conversation_id, body.text, by,
                hold_for=hold_after_writing, held_by=reader(person),
            )  # fmt: skip
            h = desk.holding(conn, t, conversation_id)
            return Sent(turn_id=turn, held_until=h.until if h else None)

        try:
            return await run_in_threadpool(tx, write)
        except desk.NotFoundError as e:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
        except desk.WindowClosedError as e:
            raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e

    return router
