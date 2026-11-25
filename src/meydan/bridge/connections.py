"""Connections and their credentials, kept in the database (extra `bridge`). docs/bridges.md.

A connection is an outside system by the app's name for it. Its credentials say how it proves
itself: a shared secret (`whsec`, Standard Webhooks `v1`) or its public key (`whpk`, `v2a`), each
either **ours** (we generated it and handed it over) or **theirs** (they gave it to us). Values
are sealed with the app's `Keyring` (meydan.vault), bound to their row; lists show only a
fingerprint. A credential is valid from `valid_from` until `valid_until` (None: until retired),
so the old and the new one both work while they rotate.

    connections.ensure(conn, t, "acme")
    secret = connections.generate_secret(conn, t, keyring, "acme", by="actor:...")  # hand it over
    connections.add_public_key(conn, t, keyring, "acme", "whpk_...", endpoint_id="ep_...")
    connections.rotate(conn, t, keyring, "acme", overlap=timedelta(days=7))

Every function runs in the caller's transaction; recording who did it (audit) is the caller's.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Connection, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from meydan.bridge import NAME_MAX, Credentials, _key, new_secret, public_key
from meydan.ids import uuid7
from meydan.runtime.tables import RuntimeTables
from meydan.vault import Keyring


def _aad(credential_id: uuid.UUID) -> bytes:
    return f"connection_credential:{credential_id}".encode()


def fingerprint(value: str) -> str:
    """A short, safe name for a credential: the first 12 hex digits of its SHA-256."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class CredentialInfo:
    """A credential without its value (for lists and pages)."""

    id: uuid.UUID
    kind: str
    origin: str
    fingerprint: str
    valid_from: datetime
    valid_until: datetime | None
    created_by: str | None
    live: bool


def ensure(
    conn: Connection,
    t: RuntimeTables,
    name: str,
    *,
    endpoint_id: str | None = None,
    note: str | None = None,
) -> None:
    """Create the connection if new; set `endpoint_id` / `note` when given."""
    if not name or len(name) > NAME_MAX or "/" in name:
        raise ValueError(f"a connection name is 1 to {NAME_MAX} characters, no '/'")
    if endpoint_id is not None and "." in endpoint_id:
        raise ValueError("an endpoint id has no '.' (it is part of what v2a signs)")
    c = t.connections
    values = {"name": name}
    changes = {"updated_at": func.now()}
    if endpoint_id is not None:
        values["endpoint_id"] = changes["endpoint_id"] = endpoint_id
    if note is not None:
        values["note"] = changes["note"] = note
    conn.execute(
        pg_insert(c).values(**values).on_conflict_do_update(index_elements=[c.c.name], set_=changes)
    )


def set_status(conn: Connection, t: RuntimeTables, name: str, on: bool) -> bool:
    c = t.connections
    done = conn.execute(
        update(c)
        .where(c.c.name == name)
        .values(status="on" if on else "off", updated_at=func.now())
    )
    return done.rowcount == 1


def _add(
    conn: Connection,
    t: RuntimeTables,
    keyring: Keyring,
    name: str,
    *,
    kind: str,
    origin: str,
    value: str,
    by: str | None,
) -> uuid.UUID:
    cc = t.connection_credentials
    cid = uuid7()
    conn.execute(
        insert(cc).values(
            id=cid, connection=name, kind=kind, origin=origin, fingerprint=fingerprint(value),
            sealed=keyring.seal(value.encode(), aad=_aad(cid)), created_by=by,
        )
    )  # fmt: skip
    return cid


def generate_secret(
    conn: Connection, t: RuntimeTables, keyring: Keyring, name: str, *, by: str | None = None
) -> str:
    """A new shared secret of ours for the connection. Returned once to hand over; it can be
    revealed again later (`reveal`), since a shared secret is needed to check signatures."""
    secret = new_secret()
    _add(conn, t, keyring, name, kind="whsec", origin="ours", value=secret, by=by)
    return secret


def add_secret(
    conn: Connection,
    t: RuntimeTables,
    keyring: Keyring,
    name: str,
    secret: str,
    *,
    by: str | None = None,
) -> uuid.UUID:
    """A shared secret they generated and gave us."""
    _key(secret)
    return _add(conn, t, keyring, name, kind="whsec", origin="theirs", value=secret, by=by)


def add_public_key(
    conn: Connection,
    t: RuntimeTables,
    keyring: Keyring,
    name: str,
    key: str,
    *,
    endpoint_id: str | None = None,
    by: str | None = None,
) -> uuid.UUID:
    """Their `whpk_` public key (`v2a`). The connection needs the endpoint id they gave our
    endpoint: given here, or already set."""
    public_key(key)
    if endpoint_id is not None:
        ensure(conn, t, name, endpoint_id=endpoint_id)
    has = conn.scalar(select(t.connections.c.endpoint_id).where(t.connections.c.name == name))
    if not has:
        raise ValueError("v2a needs the connection's endpoint id")
    return _add(conn, t, keyring, name, kind="whpk", origin="theirs", value=key, by=by)


def retire(
    conn: Connection, t: RuntimeTables, credential_id: uuid.UUID, *, after: timedelta = timedelta()
) -> bool:
    """End a credential now, or `after` a while (while the other side switches to a new one)."""
    cc = t.connection_credentials
    done = conn.execute(
        update(cc)
        .where(cc.c.id == credential_id, or_(cc.c.valid_until.is_(None),
                                              cc.c.valid_until > func.now() + after))
        .values(valid_until=func.now() + after)
    )  # fmt: skip
    return done.rowcount == 1


def rotate(
    conn: Connection,
    t: RuntimeTables,
    keyring: Keyring,
    name: str,
    *,
    overlap: timedelta = timedelta(days=7),
    by: str | None = None,
) -> str:
    """A new shared secret of ours; the connection's live shared secrets stop working after
    `overlap`. Returns the new one to hand over."""
    cc = t.connection_credentials
    live = conn.scalars(
        select(cc.c.id).where(cc.c.connection == name, cc.c.kind == "whsec", _live(cc))
    )
    for cid in live.all():
        retire(conn, t, cid, after=overlap)
    return generate_secret(conn, t, keyring, name, by=by)


def _live(cc):
    return (cc.c.valid_from <= func.now()) & or_(
        cc.c.valid_until.is_(None), cc.c.valid_until > func.now()
    )


def credentials(
    conn: Connection, t: RuntimeTables, keyring: Keyring, name: str
) -> Credentials | None:
    """What the connection may prove itself with now; None if it is unknown or off."""
    c, cc = t.connections, t.connection_credentials
    row = conn.execute(select(c.c.status, c.c.endpoint_id).where(c.c.name == name)).first()
    if row is None or row.status != "on":
        return None
    rows = conn.execute(
        select(cc.c.id, cc.c.kind, cc.c.sealed).where(cc.c.connection == name, _live(cc))
    ).all()
    secrets, keys = [], []
    for r in rows:
        value = keyring.open(r.sealed, aad=_aad(r.id)).decode()
        (secrets if r.kind == "whsec" else keys).append(value)
    return Credentials(tuple(secrets), tuple(keys), row.endpoint_id)


def listing(conn: Connection, t: RuntimeTables, name: str) -> list[CredentialInfo]:
    """The connection's credentials, newest first, without their values."""
    cc = t.connection_credentials
    rows = conn.execute(
        select(cc, _live(cc).label("live"))
        .where(cc.c.connection == name)
        .order_by(cc.c.created_at.desc())
    ).all()
    return [
        CredentialInfo(r.id, r.kind, r.origin, r.fingerprint, r.valid_from, r.valid_until,
                       r.created_by, r.live)
        for r in rows
    ]  # fmt: skip


def reveal(
    conn: Connection, t: RuntimeTables, keyring: Keyring, credential_id: uuid.UUID
) -> str | None:
    """A credential's value (for the people allowed to see it; audit the reading)."""
    cc = t.connection_credentials
    row = conn.execute(select(cc.c.id, cc.c.sealed).where(cc.c.id == credential_id)).first()
    return keyring.open(row.sealed, aad=_aad(row.id)).decode() if row else None


def reseal(conn: Connection, t: RuntimeTables, keyring: Keyring) -> int:
    """Seal again with the keyring's current key what an older key sealed (after adding a new
    key). Returns how many were resealed."""
    cc = t.connection_credentials
    n = 0
    for r in conn.execute(select(cc.c.id, cc.c.sealed)).all():
        if keyring.needs_reseal(r.sealed):
            value = keyring.open(r.sealed, aad=_aad(r.id))
            conn.execute(
                update(cc).where(cc.c.id == r.id).values(sealed=keyring.seal(value, aad=_aad(r.id)))
            )
            n += 1
    return n


__all__ = [
    "CredentialInfo", "add_public_key", "add_secret", "credentials", "ensure", "fingerprint",
    "generate_secret", "listing", "reseal", "retire", "reveal", "rotate", "set_status",
]  # fmt: skip
