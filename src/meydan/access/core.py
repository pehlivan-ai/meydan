"""Assignments (data) and the OpenFGA client (decisions)."""

import asyncio
import contextlib
import hashlib
import json
import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from openfga_sdk import CreateStoreRequest, WriteAuthorizationModelRequest
from openfga_sdk.client import ClientConfiguration, OpenFgaClient
from openfga_sdk.client.models import (
    ClientBatchCheckItem,
    ClientBatchCheckRequest,
    ClientCheckRequest,
    ClientListObjectsRequest,
    ClientTuple,
    ClientWriteRequest,
)
from openfga_sdk.client.models.write_conflict_opts import (
    ClientWriteRequestOnDuplicateWrites,
    ClientWriteRequestOnMissingDeletes,
    ConflictOptions,
)
from openfga_sdk.credentials import CredentialConfiguration, Credentials
from sqlalchemy import Connection, Engine, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from meydan.runtime.tables import RuntimeTables

log = logging.getLogger("meydan.access")

# (user, relation, object), e.g. ("club:42", "club", "tournament:7")
Relation = tuple[str, str, str]


def actor_ref(actor_id: uuid.UUID) -> str:
    return f"actor:{actor_id}"


# --- assignments: data first, OpenFGA follows -------------------------------------------------


def _open(t: RuntimeTables, actor_id: uuid.UUID, role: str, obj: str):
    g = t.grants
    return (
        g.c.actor_id == actor_id,
        g.c.scope == role,
        g.c.object == obj,
        g.c.revoked_at.is_(None),
    )


def assign(
    conn: Connection,
    t: RuntimeTables,
    actor_id: uuid.UUID,
    role: str,
    obj: str,
    granted_by: uuid.UUID | None = None,
) -> bool:
    """Give `actor_id` the `role` on `obj` (e.g. "manager", "club:42"). False if they had it."""
    if conn.scalar(select(t.grants.c.id).where(*_open(t, actor_id, role, obj))) is not None:
        return False
    conn.execute(
        insert(t.grants).values(actor_id=actor_id, scope=role, object=obj, granted_by=granted_by)
    )
    conn.execute(
        insert(t.access_outbox).values(
            op="write", user=actor_ref(actor_id), relation=role, object=obj
        )
    )
    return True


def unassign(conn: Connection, t: RuntimeTables, actor_id: uuid.UUID, role: str, obj: str) -> bool:
    revoked = conn.execute(
        update(t.grants)
        .where(*_open(t, actor_id, role, obj))
        .values(revoked_at=func.clock_timestamp())
    ).rowcount
    if revoked:
        conn.execute(
            insert(t.access_outbox).values(
                op="delete", user=actor_ref(actor_id), relation=role, object=obj
            )
        )
    return bool(revoked)


def roles_of(conn: Connection, t: RuntimeTables, actor_id: uuid.UUID) -> list[tuple[str, str]]:
    g = t.grants
    rows = conn.execute(
        select(g.c.scope, g.c.object).where(
            g.c.actor_id == actor_id, g.c.object.is_not(None), g.c.revoked_at.is_(None)
        )
    )
    return [(r.scope, r.object) for r in rows]


def may_sign_in(t: RuntimeTables, roles: Iterable[str], obj: str):
    """For sign-in: whoever holds one of `roles` on `obj` may sign in (checked locally, no call)."""
    roles = tuple(roles)

    def allowed(conn: Connection, actor_id: uuid.UUID, audience: str) -> bool:
        g = t.grants
        return (
            conn.scalar(
                select(g.c.id).where(
                    g.c.actor_id == actor_id,
                    g.c.scope.in_(roles),
                    g.c.object == obj,
                    g.c.revoked_at.is_(None),
                )
            )
            is not None
        )

    return allowed


# --- the OpenFGA client -------------------------------------------------------------------------


@dataclass(frozen=True)
class AccessConfig:
    api_url: str  # e.g. http://openfga:8080 (internal network only)
    model: str | Path  # the app's model as JSON (`fga model transform --output-format json`)
    api_token: str | None = None  # OpenFGA preshared key
    store: str = "meydan"
    instance: str = "instance:main"  # the object platform-wide roles are on
    # meydan scope -> relation on `instance`, e.g. {"ops": "can_operate", "costs:read": ...}
    scopes: Mapping[str, str] = field(default_factory=dict)
    timeout_ms: int = 3000


def _tuples(rows: Iterable[Relation]) -> list[ClientTuple]:
    return [ClientTuple(user=u, relation=r, object=o) for u, r, o in rows]


class Access:
    def __init__(self, engine: Engine, tables: RuntimeTables, config: AccessConfig):
        self.engine = engine
        self.t = tables
        self.cfg = config
        self._client: OpenFgaClient | None = None

    # --- lifecycle ----------------------------------------------------------------------------

    def _new_client(self, store_id: str | None = None, model_id: str | None = None):
        credentials = (
            Credentials(
                method="api_token",
                configuration=CredentialConfiguration(api_token=self.cfg.api_token),
            )
            if self.cfg.api_token
            else None
        )
        return OpenFgaClient(
            ClientConfiguration(
                api_url=self.cfg.api_url,
                store_id=store_id,
                authorization_model_id=model_id,
                credentials=credentials,
                timeout_millisec=self.cfg.timeout_ms,
            )
        )

    def _state(self) -> dict[str, str]:
        s = self.t.access_state
        with self.engine.connect() as conn:
            return {r.key: r.value for r in conn.execute(select(s.c.key, s.c.value))}

    def _save_state(self, **values: str) -> None:
        s = self.t.access_state
        with self.engine.begin() as conn:
            for key, value in values.items():
                stmt = pg_insert(s).values(key=key, value=value)
                conn.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[s.c.key],
                        set_={"value": stmt.excluded.value, "updated_at": func.clock_timestamp()},
                    )
                )

    async def start(self) -> None:
        """Find or create the store; write the model when it changed. Call once at startup."""
        model_json = Path(self.cfg.model).read_text()
        digest = hashlib.sha256(model_json.encode()).hexdigest()
        state = await asyncio.to_thread(self._state)
        bootstrap = self._new_client()
        try:
            store_id = state.get("store_id")
            if store_id is None:
                stores = (await bootstrap.list_stores()).stores
                found = next((s for s in stores if s.name == self.cfg.store), None)
                store_id = (
                    found.id
                    if found
                    else (await bootstrap.create_store(CreateStoreRequest(name=self.cfg.store))).id
                )
            model_id = state.get("model_id")
            if model_id is None or state.get("model_hash") != digest:
                bootstrap.set_store_id(store_id)
                model = WriteAuthorizationModelRequest(**json.loads(model_json))
                model_id = (await bootstrap.write_authorization_model(model)).authorization_model_id
        finally:
            await bootstrap.close()
        await asyncio.to_thread(
            self._save_state, store_id=store_id, model_id=model_id, model_hash=digest
        )
        await self.close()  # starting again replaces the client; never leak the old connection
        self._client = self._new_client(store_id, model_id)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    @property
    def client(self) -> OpenFgaClient:
        if self._client is None:
            raise RuntimeError("Access.start() has not run")
        return self._client

    # --- sync: the outbox to OpenFGA ------------------------------------------------------------

    async def sync_once(self, batch: int = 50) -> int:
        """Mirror pending assignments to OpenFGA, oldest first. Returns how many were synced."""
        o = self.t.access_outbox

        def pending(conn: Connection):
            return conn.execute(
                select(o)
                .where(o.c.synced_at.is_(None))
                .order_by(o.c.id)
                .limit(batch)
                .with_for_update(skip_locked=True)
            ).all()

        def save(conn: Connection, row_id, values) -> None:
            conn.execute(update(o).where(o.c.id == row_id).values(values))

        conn = await asyncio.to_thread(self.engine.connect)
        synced = 0
        try:
            trans = conn.begin()
            rows = await asyncio.to_thread(pending, conn)
            for row in rows:
                one = _tuples([(row.user, row.relation, row.object)])
                body = ClientWriteRequest(
                    writes=one if row.op == "write" else None,
                    deletes=one if row.op == "delete" else None,
                )
                options = {
                    "conflict": ConflictOptions(
                        on_duplicate_writes=ClientWriteRequestOnDuplicateWrites.IGNORE,
                        on_missing_deletes=ClientWriteRequestOnMissingDeletes.IGNORE,
                    )
                }
                try:
                    await self.client.write(body, options)
                except Exception as e:  # retried next time; order is kept (oldest first)
                    log.warning("access sync failed: %s", e)
                    failed = {"attempts": o.c.attempts + 1, "last_error": str(e)[:500]}
                    await asyncio.to_thread(save, conn, row.id, failed)
                    break
                done = {"synced_at": func.clock_timestamp(), "last_error": None}
                await asyncio.to_thread(save, conn, row.id, done)
                synced += 1
            await asyncio.to_thread(trans.commit)
            return synced
        finally:
            await asyncio.to_thread(conn.close)

    async def run(self, stop: asyncio.Event, poll_s: float = 1.0) -> None:
        """Keep syncing until `stop` is set (run next to the runtime's loops)."""
        while not stop.is_set():
            try:
                await self.sync_once()
            except Exception:
                log.exception("access sync loop error")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_s)

    # --- decisions ------------------------------------------------------------------------------

    async def check(
        self, actor_id: uuid.UUID, relation: str, obj: str, context: Iterable[Relation] = ()
    ) -> bool:
        """May the actor `relation` the object? `context`: relations from your data, e.g.
        ("club:42", "club", "tournament:7"), passed along instead of copied into OpenFGA."""
        body = ClientCheckRequest(
            user=actor_ref(actor_id),
            relation=relation,
            object=obj,
            contextual_tuples=_tuples(context) or None,
        )
        return bool((await self.client.check(body)).allowed)

    async def list_objects(
        self, actor_id: uuid.UUID, relation: str, type_: str, context: Iterable[Relation] = ()
    ) -> list[str]:
        """The objects of `type_` the actor may `relation` (ids, e.g. ["club:42"])."""
        body = ClientListObjectsRequest(
            user=actor_ref(actor_id),
            relation=relation,
            type=type_,
            contextual_tuples=_tuples(context) or None,
        )
        return list((await self.client.list_objects(body)).objects)

    async def scopes(self, actor_id: uuid.UUID) -> frozenset[str]:
        """meydan scopes the actor's platform roles give (AccessConfig.scopes), in one call."""
        if not self.cfg.scopes:
            return frozenset()
        names = list(self.cfg.scopes)
        items = [
            ClientBatchCheckItem(
                user=actor_ref(actor_id),
                relation=self.cfg.scopes[name],
                object=self.cfg.instance,
                correlation_id=str(i),
            )
            for i, name in enumerate(names)
        ]
        result = await self.client.batch_check(ClientBatchCheckRequest(checks=items))
        return frozenset(names[int(r.correlation_id)] for r in result.result if r.allowed)
