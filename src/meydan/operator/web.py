"""The operator's HTTP surface (extra `api`): FastAPI routes over `meydan.operator`.

    app.include_router(operator_routes(auth, runtime, labels=...), prefix="/operator")

Reads need the operator surface and every conversation (`ops`, `conversations:read:all`);
writes (takeover, handback, messages) also need `operator:write`. Who is acting is the principal:
a person's own session, or a service key reading and writing for someone (`X-On-Behalf-Of`),
whose id keeps their own read marks and is recorded on what they do.

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
from sqlalchemy import func, insert, select

from meydan import operator
from meydan.auth.core import Principal
from meydan.auth.web import Auth
from meydan.observe import query
from meydan.observe.query import NEED_TO_KNOW, ConversationRow
from meydan.visibility import Visibility

READ = ("ops", "conversations:read:all")
WRITE = "operator:write"

Labels = Callable[[list[uuid.UUID]], dict[uuid.UUID, dict[str, Any]]]


class OperatorRow(ConversationRow):
    unread: int = 0
    taken_by: str | None = None
    taken_until: datetime | None = None
    taken_over: bool = False
    labels: dict[str, Any] = {}


class TakeoverRequest(BaseModel):
    minutes: int | None = Field(default=None, ge=1, le=7 * 24 * 60)  # None: until handed back
    replace: bool = False  # take it from someone else who has it (else 409)


class Summary(BaseModel):
    taken_over: int


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4096)
    name: str | None = Field(
        default=None, max_length=255
    )  # who is writing, as their team calls them


class Sent(BaseModel):
    turn_id: uuid.UUID
    taken_until: datetime | None


def reader(person: Principal) -> str:
    """Whose read marks: whom a service acts for, else the person's own actor."""
    return person.on_behalf_of or str(person.actor_id)


def routes(
    auth: Auth,
    runtime,
    *,
    labels: Labels | None = None,
    visibility: Visibility = NEED_TO_KNOW,
    take_over_after_writing: timedelta | None = timedelta(minutes=30),
) -> APIRouter:
    router = APIRouter(tags=["operator"])
    reads = Annotated[Principal, Depends(auth.require(*READ))]
    writes = Annotated[Principal, Depends(auth.require(*READ, WRITE))]
    engine, t = runtime.engine, runtime.t

    def tx(fn, *args):
        with engine.begin() as conn:
            return fn(conn, *args)

    def rows_of(conn, person: Principal, limit: int, only: uuid.UUID | None = None):
        rows = query.conversations(conn, t, person, limit, visibility=visibility, only=only)
        ids = [r.id for r in rows]
        return rows, operator.unread(conn, t, reader(person), ids), operator.takeovers(conn, t, ids)

    def shaped(rows, unread, taken) -> list[OperatorRow]:
        said = labels([r.actor_id for r in rows if r.actor_id]) if labels else {}
        out = []
        for r in rows:
            h = taken.get(r.id)
            out.append(
                OperatorRow(
                    **r.model_dump(),
                    unread=unread.get(r.id, 0),
                    taken_over=h is not None,
                    taken_by=h.taken_by if h else None,
                    taken_until=h.until if h else None,
                    labels=said.get(r.actor_id, {}) if r.actor_id else {},
                )
            )
        return out

    def exists_or_404(conn, conversation_id: uuid.UUID) -> None:
        cv = t.conversations
        if conn.scalar(select(cv.c.id).where(cv.c.id == conversation_id)) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")

    def audited(conn, person: Principal, action: str, conversation_id: uuid.UUID) -> None:
        """Who took over or handed back what: the operator's acts, like reads, leave a trace."""
        conn.execute(
            insert(t.audit).values(
                actor_id=person.actor_id,
                action=action,
                subject_id=conversation_id,
                on_behalf_of=person.on_behalf_of,
            )
        )

    @router.get("/conversations")
    async def conversations(person: reads, limit: int = 100) -> list[OperatorRow]:
        """The conversations, newest first, with this reader's unread count and any takeover."""
        found = await run_in_threadpool(tx, lambda c: rows_of(c, person, min(limit, 200)))
        out = shaped(*found)
        out.sort(key=lambda d: d.last_message_at or d.last_at or datetime.min, reverse=True)
        return out

    @router.get("/conversations/{conversation_id}")
    async def conversation(person: reads, conversation_id: uuid.UUID) -> OperatorRow:
        """One conversation's row (who, unread, takeover), however far down the list it is."""
        found = await run_in_threadpool(tx, lambda c: rows_of(c, person, 1, conversation_id))
        out = shaped(*found)
        if not out:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
        return out[0]

    @router.get("/summary")
    async def summary(person: reads) -> Summary:
        """Counts only, nothing read: how many conversations are taken over now."""

        def count(conn):
            h = t.operator_holds
            n = conn.scalar(
                select(func.count()).where((h.c.until.is_(None)) | (h.c.until > func.now()))
            )
            return Summary(taken_over=int(n or 0))

        return await run_in_threadpool(tx, count)

    @router.post("/conversations/{conversation_id}/read", status_code=204)
    async def read(person: reads, conversation_id: uuid.UUID) -> None:
        def mark(conn):
            exists_or_404(conn, conversation_id)
            operator.mark_read(conn, t, conversation_id, reader(person))

        await run_in_threadpool(tx, mark)

    @router.post("/conversations/{conversation_id}/takeover")
    async def takeover(
        person: writes, conversation_id: uuid.UUID, body: TakeoverRequest
    ) -> operator.Takeover:
        span = timedelta(minutes=body.minutes) if body.minutes else None

        def take(conn):
            exists_or_404(conn, conversation_id)
            got = operator.takeover(
                conn, t, conversation_id, reader(person), span, replace=body.replace
            )
            audited(conn, person, "operator:takeover", conversation_id)
            return got

        try:
            return await run_in_threadpool(tx, take)
        except operator.AlreadyTakenError as e:
            raise HTTPException(status.HTTP_409_CONFLICT, "taken over by someone else") from e

    @router.post("/conversations/{conversation_id}/handback", status_code=204)
    async def handback(person: writes, conversation_id: uuid.UUID) -> None:
        def give(conn):
            exists_or_404(conn, conversation_id)
            operator.handback(conn, t, conversation_id)
            audited(conn, person, "operator:handback", conversation_id)

        await run_in_threadpool(tx, give)

    @router.post("/conversations/{conversation_id}/messages")
    async def message(person: writes, conversation_id: uuid.UUID, body: MessageRequest) -> Sent:
        def write(conn):
            by = (
                operator.staff_actor(conn, t, person.on_behalf_of, body.name)
                if person.on_behalf_of
                else person.actor_id
            )
            turn = operator.send(
                conn, runtime, conversation_id, body.text, by,
                take_over_for=take_over_after_writing, taken_by=reader(person),
            )  # fmt: skip
            h = operator.takeover_of(conn, t, conversation_id)
            return Sent(turn_id=turn, taken_until=h.until if h else None)

        try:
            return await run_in_threadpool(tx, write)
        except operator.NotFoundError as e:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
        except operator.WindowClosedError as e:
            raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e

    return router
