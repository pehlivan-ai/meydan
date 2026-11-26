"""Managing connections over HTTP (extras `api`, `bridge`): what a connection's own page, or the
team's, needs. docs/bridges.md.

    app.include_router(
        manage_routes(bridge, auth, may=may, audit=audit), prefix="/connections"
    )

Who may do what is the app's: `may(principal, connection, action)` answers for `read` (see it,
its credentials without values, its events and refused requests) and `manage` (create, accept,
rotate, reveal, retire credentials; add a public key, a token or an hmac secret of theirs; retry
an event; switch it on or off). It may be async (an OpenFGA check, say). `names(principal)` lists
the connections to show (default: every connection the principal may read). `audit(conn,
principal, action, connection)` records each change and each reveal, in the change's own
transaction; actions are `secret:ours`, `secret:theirs`, `token:theirs`, `hmac:theirs`,
`public-key`, `rotate`, `reveal`, `retire`, `retry`, `on`, `off`.

A generated or rotated secret is returned once in the answer. Nothing else carries a value:
lists show kind, origin, fingerprint and validity. Showing a stored secret again is **off by
default** (`reveal=False`): nobody, the app's own admins included, can read one back over HTTP,
for apps that want it that way. An app that wants its people to see them passes `reveal=True`
(each reveal is audited). Stricter still: their public keys only (`v2a`), so no secret is held.
Nothing is allowed by default either: `may` decides everything, for admins too.
"""

import inspect
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import Connection, select

from meydan.auth.core import Principal
from meydan.auth.web import Auth
from meydan.bridge import Bridge
from meydan.bridge import connections as cx

Action = Literal["read", "manage"]
May = Callable[[Principal, str, Action], "bool | Awaitable[bool]"]
Names = Callable[[Principal], "list[str] | Awaitable[list[str]]"]
Audit = Callable[[Connection, Principal, str, str], None]


class CredentialView(BaseModel):
    id: uuid.UUID
    # whsec (shared secret, v1) | whpk (their public key, v2a): what their requests are checked
    # with; token (we present it to their API) | hmac (they sign their results with it)
    kind: str
    origin: str  # ours | theirs
    fingerprint: str
    valid_from: datetime
    valid_until: datetime | None
    live: bool


class ConnectionView(BaseModel):
    name: str
    status: str  # on | off
    endpoint_id: str | None
    note: str | None
    credentials: list[CredentialView]


class EventView(BaseModel):
    id: uuid.UUID
    event_id: str
    type: str
    status: str  # pending | done | ignored | dead
    attempts: int
    sent_at: datetime
    received_at: datetime
    handled_at: datetime | None
    last_error: str | None
    payload: dict[str, Any]


class RejectionView(BaseModel):
    status: int
    reason: str
    webhook_id: str | None
    at: datetime


class SecretIn(BaseModel):
    """No `secret`: we generate a shared secret (ours). With `secret`: theirs, given to us; `kind`
    says what it is: a shared secret their requests are signed with (`whsec`), a `token` we
    present to their API, or the `hmac` secret they sign their results with. Only a shared secret
    can be generated here; a token or an hmac secret is theirs to give."""

    kind: Literal["whsec", "token", "hmac"] = "whsec"
    secret: str | None = Field(default=None, max_length=cx.VALUE_MAX)


class PublicKeyIn(BaseModel):
    key: str = Field(max_length=100)
    endpoint_id: str | None = Field(default=None, max_length=64)


class RotateIn(BaseModel):
    overlap_days: float = Field(default=7, ge=0, le=90)


class RetireIn(BaseModel):
    after_days: float = Field(default=0, ge=0, le=90)


class StatusIn(BaseModel):
    on: bool


class SecretOut(BaseModel):
    fingerprint: str
    secret: str | None = None  # only when we generated it (shown once) or it is revealed


async def _maybe(value):
    return await value if inspect.isawaitable(value) else value


def routes(
    bridge: Bridge,
    auth: Auth,
    *,
    may: May,
    audit: Audit,
    names: Names | None = None,
    reveal: bool = False,
) -> APIRouter:
    show_again = reveal  # (the route below is a function; keep the flag under its own name)
    if bridge.keyring is None:
        raise ValueError("managing connections needs the bridge's keyring")
    t, keyring, engine = bridge.t, bridge.keyring, bridge.engine
    router = APIRouter(tags=["connections"])
    signed_in = Annotated[Principal, Depends(auth.signed_in)]

    async def allowed(person: Principal, name: str, action: Action) -> None:
        if not await _maybe(may(person, name, action)):
            raise HTTPException(404, "No such connection.")  # not 403: names stay private

    def view(conn: Connection, name: str) -> ConnectionView | None:
        c = t.connections
        row = conn.execute(select(c).where(c.c.name == name)).first()
        if row is None:
            return None
        creds = [CredentialView(**{k: getattr(i, k) for k in CredentialView.model_fields})
                 for i in cx.listing(conn, t, name)]  # fmt: skip
        return ConnectionView(name=row.name, status=row.status, endpoint_id=row.endpoint_id,
                              note=row.note, credentials=creds)  # fmt: skip

    def one(name: str) -> ConnectionView:
        with engine.connect() as conn:
            found = view(conn, name)
        if found is None:
            raise HTTPException(404, "No such connection.")
        return found

    def change(person: Principal, name: str, action: str, fn):
        """Run `fn(conn)` and record it, in one transaction."""
        with engine.begin() as conn:
            if view(conn, name) is None:
                raise HTTPException(404, "No such connection.")
            try:
                out = fn(conn)
            except ValueError as e:
                raise HTTPException(422, str(e)) from None
            audit(conn, person, action, name)
            return out

    @router.get("")
    async def list_connections(person: signed_in) -> list[ConnectionView]:
        """The connections this person may see."""
        if names is not None:
            wanted = await _maybe(names(person))
        else:

            def all_names():
                with engine.connect() as conn:
                    return list(conn.scalars(select(t.connections.c.name)))

            wanted = [n for n in await run_in_threadpool(all_names)
                      if await _maybe(may(person, n, "read"))]  # fmt: skip

        def views():
            with engine.connect() as conn:
                return [v for n in sorted(wanted) if (v := view(conn, n)) is not None]

        return await run_in_threadpool(views)

    @router.get("/{name}")
    async def get_connection(name: str, person: signed_in) -> ConnectionView:
        await allowed(person, name, "read")
        return await run_in_threadpool(one, name)

    @router.get("/{name}/events")
    async def events(
        name: str, person: signed_in, limit: Annotated[int, Query(ge=1, le=200)] = 50
    ) -> list[EventView]:
        """Its latest events, newest first, with what became of each."""
        await allowed(person, name, "read")
        b = t.bridge_events

        def rows():
            with engine.connect() as conn:
                return conn.execute(
                    select(b).where(b.c.connection == name)
                    .order_by(b.c.received_at.desc()).limit(limit)
                ).all()  # fmt: skip

        return [EventView(**{k: getattr(r, k) for k in EventView.model_fields})
                for r in await run_in_threadpool(rows)]  # fmt: skip

    @router.get("/{name}/rejections")
    async def rejections(
        name: str, person: signed_in, limit: Annotated[int, Query(ge=1, le=200)] = 20
    ) -> list[RejectionView]:
        """Its latest refused requests and why (no bodies are kept)."""
        await allowed(person, name, "read")
        r = t.bridge_rejections

        def rows():
            with engine.connect() as conn:
                return conn.execute(
                    select(r).where(r.c.connection == name).order_by(r.c.at.desc()).limit(limit)
                ).all()

        return [RejectionView(status=x.status, reason=x.reason, webhook_id=x.webhook_id, at=x.at)
                for x in await run_in_threadpool(rows)]  # fmt: skip

    @router.post("/{name}/events/{event_id}/retry")
    async def retry(name: str, event_id: uuid.UUID, person: signed_in) -> ConnectionView:
        await allowed(person, name, "manage")

        def go():
            def fn(conn):
                b = t.bridge_events
                mine = conn.scalar(select(b.c.id).where(b.c.id == event_id, b.c.connection == name))
                if mine is None or not bridge.retry(conn, event_id):
                    raise ValueError("only a dead or ignored event of this connection")

            change(person, name, "retry", fn)
            return one(name)

        return await run_in_threadpool(go)

    @router.post("/{name}/secrets")
    async def add_secret(name: str, body: SecretIn, person: signed_in) -> SecretOut:
        """Ours (no `secret`: a shared secret, generated, returned once) or theirs (`secret`, of
        `kind` whsec, token or hmac: theirs to keep, not returned)."""
        await allowed(person, name, "manage")
        by = f"actor:{person.actor_id}"
        if body.kind != "whsec" and not body.secret:
            raise HTTPException(422, f"A {body.kind} is theirs: send it as `secret`.")

        def go():
            if body.secret:
                theirs, kind = body.secret, body.kind

                def store(conn):
                    cx.add_credential(conn, t, keyring, name, kind, theirs, by=by)

                action = "secret:theirs" if kind == "whsec" else f"{kind}:theirs"
                change(person, name, action, store)
                return SecretOut(fingerprint=cx.fingerprint(theirs))

            def make(conn):
                return cx.generate_secret(conn, t, keyring, name, by=by)

            secret = change(person, name, "secret:ours", make)
            return SecretOut(fingerprint=cx.fingerprint(secret), secret=secret)

        return await run_in_threadpool(go)

    @router.post("/{name}/public-keys")
    async def add_public_key(name: str, body: PublicKeyIn, person: signed_in) -> SecretOut:
        """Their `whpk_` public key (v2a), with the endpoint id they gave our endpoint."""
        await allowed(person, name, "manage")
        by = f"actor:{person.actor_id}"

        def go():
            change(person, name, "public-key", lambda conn: cx.add_public_key(
                conn, t, keyring, name, body.key, endpoint_id=body.endpoint_id, by=by))  # fmt: skip
            return SecretOut(fingerprint=cx.fingerprint(body.key))

        return await run_in_threadpool(go)

    @router.post("/{name}/rotate")
    async def rotate(name: str, body: RotateIn, person: signed_in) -> SecretOut:
        """A new secret of ours (returned once); the live ones end after `overlap_days`."""
        await allowed(person, name, "manage")
        by = f"actor:{person.actor_id}"
        overlap = timedelta(days=body.overlap_days)

        def go():
            def fn(conn):
                return cx.rotate(conn, t, keyring, name, overlap=overlap, by=by)

            secret = change(person, name, "rotate", fn)
            return SecretOut(fingerprint=cx.fingerprint(secret), secret=secret)

        return await run_in_threadpool(go)

    def _credential(conn: Connection, name: str, credential_id: uuid.UUID) -> None:
        cc = t.connection_credentials
        found = conn.scalar(
            select(cc.c.id).where(cc.c.id == credential_id, cc.c.connection == name)
        )
        if found is None:
            raise ValueError("no such credential on this connection")

    @router.post("/{name}/credentials/{credential_id}/reveal")
    async def reveal_credential(
        name: str, credential_id: uuid.UUID, person: signed_in
    ) -> SecretOut:
        """One credential's value again (audited). Off unless the app passed `reveal=True`."""
        if not show_again:
            raise HTTPException(404, "Showing a stored secret is off here.")
        await allowed(person, name, "manage")

        def go():
            def fn(conn):
                _credential(conn, name, credential_id)
                return cx.reveal(conn, t, keyring, credential_id)

            value = change(person, name, "reveal", fn)
            return SecretOut(fingerprint=cx.fingerprint(value), secret=value)

        return await run_in_threadpool(go)

    @router.post("/{name}/credentials/{credential_id}/retire")
    async def retire(
        name: str, credential_id: uuid.UUID, body: RetireIn, person: signed_in
    ) -> ConnectionView:
        """End one credential now, or after `after_days`."""
        await allowed(person, name, "manage")

        def go():
            def fn(conn):
                _credential(conn, name, credential_id)
                cx.retire(conn, t, credential_id, after=timedelta(days=body.after_days))

            change(person, name, "retire", fn)
            return one(name)

        return await run_in_threadpool(go)

    @router.post("/{name}/status")
    async def set_status(name: str, body: StatusIn, person: signed_in) -> ConnectionView:
        """Switch it on or off (off: its requests are answered 404, and `connections.value`
        gives none of its tokens or hmac secrets)."""
        await allowed(person, name, "manage")

        def go():
            change(person, name, "on" if body.on else "off",
                   lambda conn: cx.set_status(conn, t, name, body.on))  # fmt: skip
            return one(name)

        return await run_in_threadpool(go)

    return router


__all__ = [
    "Action", "Audit", "ConnectionView", "CredentialView", "EventView", "May", "Names",
    "RejectionView", "SecretOut", "routes",
]  # fmt: skip
