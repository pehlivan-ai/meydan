"""Bridges: facts an outside system tells an instance (extra `runtime`; routes need `api`).

An outside system (a stakeholder's software, a payment provider, a booking tool, another
instance) is a **connection**: a name the app chooses and its credentials (a shared secret, or
their public key; from config or, with a keyring, from `meydan.bridge.connections`). It tells us
what happened as **events**, signed by [Standard Webhooks](https://www.standardwebhooks.com/)
(`webhook-id`, `webhook-timestamp`, `webhook-signature`; `v1` HMAC or `v2a` ed25519). An event
is verified and stored once before we answer; a worker then hands it to the app's handler for
its (connection, type). docs/bridges.md.

    bridge = Bridge(engine, t, keyring=Keyring.from_env(),
                    handlers={("acme", "payment.succeeded"): payments.on_paid})
    app.include_router(bridge_routes(bridge), prefix="/connections")  # POST /connections/acme/...
    asyncio.create_task(bridge.run())

    def on_paid(conn, event):          # the app's meaning, in the event's transaction
        ...                            # raise PermanentError("unknown ref"): no retry

Guarantees: only the holder of a connection's credential can speak for it; a signed request is
good for `tolerance` (no replays); an event is stored once per (connection, event id) and handled at
most once to completion; a failing handler's writes are rolled back and it is tried again later,
then given up (`dead`) with its reason; an event nobody handles is kept as `ignored`.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets as _secrets
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from meydan.runtime.tables import RuntimeTables

if TYPE_CHECKING:
    from meydan.vault import Keyring

log = logging.getLogger(__name__)

SECRET_PREFIX = "whsec_"
TOLERANCE = timedelta(minutes=5)
ATTEMPTS = 8
NAME_MAX = 64
TYPE_MAX = 120
ID_MAX = 255


# --- Standard Webhooks signatures -------------------------------------------------------------


def new_secret() -> str:
    """A fresh secret to share with a connection: `whsec_` + base64 of 32 random bytes."""
    return SECRET_PREFIX + base64.b64encode(_secrets.token_bytes(32)).decode()


def _key(secret: str) -> bytes:
    """The HMAC key: the base64 after `whsec_`, 24 to 64 bytes (the spec's range)."""
    raw = secret.removeprefix(SECRET_PREFIX)
    try:
        key = base64.b64decode(raw, validate=True)
    except ValueError as e:
        raise ValueError("a bridge secret is base64, optionally prefixed with whsec_") from e
    if not 24 <= len(key) <= 64:
        raise ValueError("a bridge secret is 24 to 64 random bytes")
    return key


def sign(secret: str, event_id: str, timestamp: int, body: bytes) -> str:
    """The `webhook-signature` value for a body: `v1,<base64 HMAC-SHA256>` of
    `<id>.<timestamp>.<body>`."""
    content = f"{event_id}.{timestamp}.".encode() + body
    mac = hmac.new(_key(secret), content, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


SIGNING_KEY_PREFIX = "whsk_"
PUBLIC_KEY_PREFIX = "whpk_"


def new_keypair() -> tuple[str, str]:
    """A sender's ed25519 pair for `v2a`: (`whsk_` signing key, kept by the sender; `whpk_`
    public key, given to the receiver). Needs the `bridge` extra."""
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    seed = sk.private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption())
    pub = sk.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    return (
        SIGNING_KEY_PREFIX + base64.b64encode(seed).decode(),
        PUBLIC_KEY_PREFIX + base64.b64encode(pub).decode(),
    )


def _raw32(value: str, prefix: str, what: str) -> bytes:
    try:
        raw = base64.b64decode(value.removeprefix(prefix), validate=True)
    except ValueError as e:
        raise ValueError(f"a {what} is base64, prefixed with {prefix}") from e
    if len(raw) != 32:
        raise ValueError(f"a {what} is 32 bytes")
    return raw


def _v2a_content(endpoint_id: str, event_id: str, timestamp: int, body: bytes) -> bytes:
    """What `v2a` signs: SHA-256 of `<endpoint id>.<id>.<timestamp>.<body>` (the raw 32 bytes)."""
    return hashlib.sha256(f"{endpoint_id}.{event_id}.{timestamp}.".encode() + body).digest()


def sign_v2a(signing_key: str, endpoint_id: str, event_id: str, timestamp: int, body: bytes) -> str:
    """The `v2a,<base64 ed25519>` signature (for senders written in Python and tests)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.from_private_bytes(
        _raw32(signing_key, SIGNING_KEY_PREFIX, "signing key")
    )
    sig = sk.sign(_v2a_content(endpoint_id, event_id, timestamp, body))
    return "v2a," + base64.b64encode(sig).decode()


def public_key(value: str) -> str:
    """Check a `whpk_` public key (32 bytes) and return it as given."""
    _raw32(value, PUBLIC_KEY_PREFIX, "public key")
    return value


def headers(
    secret: str | None,
    body: bytes,
    *,
    event_id: str | None = None,
    at: int | None = None,
    signing_key: str | None = None,
    endpoint_id: str | None = None,
) -> dict[str, str]:
    """The three headers a sender puts on a request (for senders written in Python and tests).
    `secret` signs `v1`; `signing_key` (with `endpoint_id`) signs `v2a`; both: both."""
    event_id = event_id or f"msg_{uuid.uuid4().hex}"
    at = int(time.time()) if at is None else at
    sigs = []
    if secret:
        sigs.append(sign(secret, event_id, at, body))
    if signing_key:
        if not endpoint_id:
            raise ValueError("v2a signs the endpoint id: give endpoint_id")
        sigs.append(sign_v2a(signing_key, endpoint_id, event_id, at, body))
    if not sigs:
        raise ValueError("give a secret or a signing key")
    return {
        "webhook-id": event_id,
        "webhook-timestamp": str(at),
        "webhook-signature": " ".join(sigs),
    }


@dataclass(frozen=True)
class Credentials:
    """What a connection may prove itself with right now: shared secrets (`v1`) and public keys
    (`v2a`, with the endpoint id they sign). More than one of each while they are rotated."""

    secrets: tuple[str, ...] = ()
    public_keys: tuple[str, ...] = ()
    endpoint_id: str | None = None

    def __bool__(self) -> bool:
        return bool(self.secrets or (self.public_keys and self.endpoint_id))


class RejectedError(Exception):
    """The request is not taken. `status` is the HTTP answer to give."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


def verify(
    credentials: "Credentials | Sequence[str]",
    head: Mapping[str, str],
    body: bytes,
    *,
    now: float | None = None,
    tolerance: timedelta = TOLERANCE,
) -> tuple[str, datetime]:
    """Check a request against any of a connection's credentials: a `v1` signature by one of its
    secrets, or a `v2a` signature by one of its public keys. Versions it has no credential for
    are ignored. Returns the event id and its signed time; raises `RejectedError` (400 or 401)."""
    creds = credentials if isinstance(credentials, Credentials) else Credentials(tuple(credentials))
    h = {k.lower(): v for k, v in head.items()}
    event_id, stamp, given = (
        h.get("webhook-id"),
        h.get("webhook-timestamp"),
        h.get("webhook-signature"),
    )
    if not event_id or not stamp or not given:
        raise RejectedError(400, "missing webhook headers")
    if len(event_id) > ID_MAX:
        raise RejectedError(400, "webhook-id too long")
    try:
        at = int(stamp)
    except ValueError:
        raise RejectedError(400, "bad webhook-timestamp") from None
    now = time.time() if now is None else now
    if abs(now - at) > tolerance.total_seconds():
        raise RejectedError(401, "timestamp outside tolerance")
    offered: dict[str, list[str]] = {}
    for entry in given.split():
        version, _, sig = entry.partition(",")
        offered.setdefault(version, []).append(sig)
    signed_at = datetime.fromtimestamp(at, UTC)
    for secret in creds.secrets:
        expected = sign(secret, event_id, at, body).split(",", 1)[1]
        if any(hmac.compare_digest(expected, o) for o in offered.get("v1", ())):
            return event_id, signed_at
    if creds.public_keys and creds.endpoint_id and offered.get("v2a"):
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        content = _v2a_content(creds.endpoint_id, event_id, at, body)
        for pk in creds.public_keys:
            key = Ed25519PublicKey.from_public_bytes(_raw32(pk, PUBLIC_KEY_PREFIX, "public key"))
            for sig in offered["v2a"]:
                try:
                    key.verify(base64.b64decode(sig), content)
                    return event_id, signed_at
                except (InvalidSignature, ValueError):
                    continue
    raise RejectedError(401, "bad signature")


# --- Events -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """One stored event. `data` is the body's `data` (Standard Webhooks' payload shape:
    `{"type", "timestamp", "data"}`), or the whole body when it has none."""

    id: uuid.UUID
    connection: str
    event_id: str
    type: str
    data: dict[str, Any]
    payload: dict[str, Any]
    sent_at: datetime
    attempts: int

    @classmethod
    def of(cls, row: Any) -> "Event":
        p = dict(row.payload or {})
        data = p.get("data")
        return cls(
            id=row.id, connection=row.connection, event_id=row.event_id, type=row.type,
            data=dict(data) if isinstance(data, dict) else p, payload=p, sent_at=row.sent_at,
            attempts=row.attempts,
        )  # fmt: skip


class PermanentError(Exception):
    """Raised by a handler: this event can never be handled (an unknown reference, say). It is
    marked `dead` with the reason, without retrying."""


Handler = Callable[[Connection, Event], None]


@dataclass(frozen=True)
class Received:
    id: uuid.UUID | None  # our id for the event; None when it was a repeat
    duplicate: bool


class Bridge:
    """Receives events for the app's connections and hands them to its handlers.

    Credentials come from `secrets` (connection name -> a secret, or several while one is
    rotated: simple, from config) and, with a `keyring`, from the connections table
    (meydan.bridge.connections: shared secrets and public keys, ours or theirs, sealed). Config
    wins for a name it has. A connection with neither is unknown (404). `handlers`:
    (connection, type) -> handler."""

    def __init__(
        self,
        engine: Engine,
        t: RuntimeTables,
        *,
        handlers: Mapping[tuple[str, str], Handler],
        secrets: Mapping[str, str | Sequence[str]] | None = None,
        keyring: "Keyring | None" = None,
        tolerance: timedelta = TOLERANCE,
        attempts: int = ATTEMPTS,
    ):
        self.keyring = keyring
        secrets = secrets or {}
        self.engine, self.t = engine, t
        self.secrets: dict[str, tuple[str, ...]] = {}
        for name, s in secrets.items():
            if len(name) > NAME_MAX:
                raise ValueError(f"connection name longer than {NAME_MAX}: {name!r}")
            keys = (s,) if isinstance(s, str) else tuple(s)
            keys = tuple(k for k in keys if k)
            for k in keys:
                _key(k)  # fail at start, not on the first request
            if keys:
                self.secrets[name] = keys
        self.handlers = dict(handlers)
        self.tolerance, self.attempts = tolerance, attempts

    def receive(
        self,
        conn: Connection,
        connection: str,
        head: Mapping[str, str],
        body: bytes,
        *,
        now: float | None = None,
    ) -> Received:
        """Verify and store one request, inside the caller's transaction. Cheap: call it before
        answering. Raises `RejectedError` (404 unknown connection, 400 malformed, 401 not
        signed)."""
        creds = self.credentials(conn, connection)
        if not creds:
            raise RejectedError(404, "unknown connection")
        event_id, sent_at = verify(creds, head, body, now=now, tolerance=self.tolerance)
        try:
            payload = json.loads(body)
        except ValueError:
            raise RejectedError(400, "body is not JSON") from None
        if not isinstance(payload, dict):
            raise RejectedError(400, "body is not a JSON object")
        kind = payload.get("type")
        if not isinstance(kind, str) or not kind or len(kind) > TYPE_MAX:
            raise RejectedError(400, "missing or bad type")
        b = self.t.bridge_events
        status = "pending" if (connection, kind) in self.handlers else "ignored"
        new = conn.execute(
            pg_insert(b)
            .values(
                connection=connection, event_id=event_id, type=kind, payload=payload,
                sent_at=sent_at, status=status,
                handled_at=func.now() if status == "ignored" else None,
            )
            .on_conflict_do_nothing(index_elements=[b.c.connection, b.c.event_id])
            .returning(b.c.id)
        ).scalar()  # fmt: skip
        if new is None:
            return Received(None, True)
        if status == "ignored":
            log.info("bridge: no handler for %s/%s, kept as ignored", connection, kind)
        return Received(new, False)

    def credentials(self, conn: Connection, connection: str) -> Credentials | None:
        if keys := self.secrets.get(connection):
            return Credentials(keys)
        if self.keyring is None:
            return None
        from meydan.bridge import connections

        return connections.credentials(conn, self.t, self.keyring, connection)

    # --- the worker ---

    def drain(self, limit: int = 50) -> int:
        """Handle what is due, one event per transaction (locked while handled, so two workers
        never handle the same one). Returns how many were tried."""
        tried = 0
        for _ in range(limit):
            if not self._one():
                break
            tried += 1
        return tried

    def _one(self) -> bool:
        b = self.t.bridge_events
        with self.engine.begin() as conn:
            row = conn.execute(
                select(b)
                .where(b.c.status == "pending", b.c.next_at <= func.now())
                .order_by(b.c.next_at, b.c.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first()
            if row is None:
                return False
            event = Event.of(row)
            this = b.c.id == event.id
            handler = self.handlers.get((event.connection, event.type))
            if handler is None:  # the app stopped handling this type since it arrived
                conn.execute(update(b).where(this).values(status="ignored", handled_at=func.now()))
                return True
            tries = event.attempts + 1
            try:
                with conn.begin_nested():  # the handler's writes go only if it succeeds
                    handler(conn, event)
            except PermanentError as e:
                log.warning("bridge: %s/%s refused: %s", event.connection, event.type, e)
                conn.execute(update(b).where(this).values(
                    status="dead", attempts=tries, handled_at=func.now(), last_error=str(e)[:500],
                ))  # fmt: skip
                return True
            except Exception as e:
                error = f"{type(e).__name__}: {e}"[:500]
                if tries >= self.attempts:
                    log.error("bridge: %s/%s gave up: %s", event.connection, event.type, error)
                    conn.execute(update(b).where(this).values(
                        status="dead", attempts=tries, handled_at=func.now(), last_error=error,
                    ))  # fmt: skip
                else:
                    log.warning("bridge: %s/%s failed (try %d): %s",
                                event.connection, event.type, tries, error)  # fmt: skip
                    wait = timedelta(seconds=min(30 * 2 ** (tries - 1), 6 * 3600))
                    conn.execute(update(b).where(this).values(
                        attempts=tries, last_error=error, next_at=func.now() + wait,
                    ))  # fmt: skip
                return True
            conn.execute(update(b).where(this).values(
                status="done", attempts=tries, handled_at=func.now(), last_error=None,
            ))  # fmt: skip
        return True

    async def run(self, every: float = 1.0) -> None:
        """Drain forever (cancel the task to stop)."""
        while True:
            try:
                await asyncio.to_thread(self.drain)
            except Exception:
                log.exception("bridge: drain failed")
            await asyncio.sleep(every)

    def retry(self, conn: Connection, event_id: uuid.UUID) -> bool:
        """Put a dead or ignored event back in line (after fixing what made it fail)."""
        b = self.t.bridge_events
        done = conn.execute(
            update(b)
            .where(b.c.id == event_id, b.c.status.in_(("dead", "ignored")))
            .values(status="pending", attempts=0, next_at=func.now(), handled_at=None)
        )
        return done.rowcount == 1


__all__ = [
    "ATTEMPTS", "PUBLIC_KEY_PREFIX", "SECRET_PREFIX", "SIGNING_KEY_PREFIX", "TOLERANCE", "Bridge",
    "Credentials", "Event", "Handler", "PermanentError", "Received", "RejectedError", "headers",
    "new_keypair", "new_secret", "public_key", "sign", "sign_v2a", "verify",
]  # fmt: skip
