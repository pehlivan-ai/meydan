"""HTTP surface for the read models (extra `api`): mount next to meydan.auth.web.

    app.include_router(observe_routes(auth, Visibility.trusted("ops")), prefix="/observe")

Every endpoint needs a signed-in principal; scopes are enforced by the read models themselves
(403), and a conversation you may not see is a 404, like one that doesn't exist.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from meydan.auth.core import Principal
from meydan.auth.web import Auth
from meydan.observe import query
from meydan.observe.query import (
    NEED_TO_KNOW,
    ConversationDetail,
    ConversationRow,
    CostReport,
    NotAllowedError,
    Overview,
    Problem,
    TemplateRow,
)
from meydan.visibility import Visibility


def routes(auth: Auth, visibility: Visibility = NEED_TO_KNOW) -> APIRouter:
    """The read models over HTTP, showing what `visibility` allows each person."""
    router = APIRouter(tags=["observe"])
    signed_in = Annotated[Principal, Depends(auth.signed_in)]

    def read(fn, *args, **kwargs):
        try:
            with auth.engine.begin() as conn:
                return fn(conn, auth.t, *args, visibility=visibility, **kwargs)
        except NotAllowedError as e:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed.") from e

    @router.get("/overview")
    def overview(
        person: signed_in, hours: Annotated[int, Query(ge=1, le=24 * 30)] = 24
    ) -> Overview:
        """Turns, cost, latency and delivery health over the last hours."""
        return read(query.overview, person, hours)

    @router.get("/conversations")
    def conversations(
        person: signed_in, limit: Annotated[int, Query(ge=1, le=200)] = 50
    ) -> list[ConversationRow]:
        """Conversations, most recent first (yours only, unless you may read all)."""
        return read(query.conversations, person, limit)

    @router.get("/conversations/{conversation_id}")
    def conversation(person: signed_in, conversation_id: uuid.UUID) -> ConversationDetail:
        """Transcript and each turn's timeline, tokens and cost."""
        found = read(query.conversation, person, conversation_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
        return found

    @router.get("/costs")
    def costs(person: signed_in, days: Annotated[int, Query(ge=1, le=366)] = 30) -> CostReport:
        """Cost per day and per model."""
        return read(query.costs, person, days)

    @router.get("/problems")
    def problems(
        person: signed_in, hours: Annotated[int, Query(ge=1, le=24 * 30)] = 72
    ) -> list[Problem]:
        """Failed turns and dead letters, newest first."""
        return read(query.problems, person, hours)

    @router.get("/templates")
    def templates(person: signed_in) -> list[TemplateRow]:
        """Message templates: review status, category, quality, messages held for them."""
        return read(query.templates, person)

    return router
