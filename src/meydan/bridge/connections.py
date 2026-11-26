"""Connections and their credentials, kept in the database (extra `bridge`). docs/bridges.md.

A connection is an outside system by the app's name for it. Its credentials say how it proves
itself to us: a shared secret (`whsec`, Standard Webhooks `v1`) or its public key (`whpk`,
`v2a`); and, when we call it or check what it signs its own way, what it gave us for that: a
`token` we present to its API, an `hmac` secret it signs its results with. Each is either
**ours** (we generated it and handed it over) or **theirs** (they gave it to us). Values are
sealed with the app's `Keyring` (meydan.vault), bound to their row; lists show only a
fingerprint. A credential is valid from `valid_from` until `valid_until` (None: until retired),
so the old and the new one both work while they rotate. Only `whsec` and `whpk` ever verify a
request to us (`credentials`); a `token` or an `hmac` is read with `value`.

    connections.ensure(conn, t, "acme")
    secret = connections.generate_secret(conn, t, keyring, "acme", by="actor:...")  # hand it over
    connections.add_public_key(conn, t, keyring, "acme", "whpk_...", endpoint_id="ep_...")
    connections.rotate(conn, t, keyring, "acme", overlap=timedelta(days=7))
    connections.add_credential(conn, t, keyring, "acme", "token", "...")  # theirs, for their API
    token = connections.value(conn, t, keyring, "acme", "token")  # newest live one, or None

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
from meydan.runtime.tables import (
    CREDENTIAL_KINDS,
    CREDENTIAL_ORIGINS,
    INBOUND_CREDENTIAL_KINDS,
    RuntimeTables,
)
from meydan.vault import Keyring

# The longest value a credential may have (a long API token fits).
VALUE_MAX = 4096


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
    valid_until: datetime | None = None,
) -> uuid.UUID:
    cc = t.connection_credentials
    cid = uuid7()
    conn.execute(
        insert(cc).values(
            id=cid, connection=name, kind=kind, origin=origin, fingerprint=fingerprint(value),
            sealed=keyring.seal(value.encode(), aad=_aad(cid)), created_by=by,
            valid_until=valid_until,
        )
    )  # fmt: skip
    return cid


def add_credential(
    conn: Connection,
    t: RuntimeTables,
    keyring: Keyring,
    name: str,
    kind: str,
    value: str,
    *,
    origin: str = "theirs",
    by: str | None = None,
    valid_until: datetime | None = None,
) -> uuid.UUID:
    """Keep one credential of any kind, sealed: usually one they gave us (`origin="theirs"`), such
    as a `token` for their API or the `hmac` secret they sign their results with. A `whsec` or
    `whpk` is checked as `add_secret` / `add_public_key` check it (a `whpk` needs the
    connection's endpoint id); any value is 1 to `VALUE_MAX` characters with nothing blank around
    it (a pasted line break would fail much later, far from here). `valid_until` (timezone-aware)
    ends it then; None: until retired. To replace one, add the new one (the newest live one is
    what `value` gives), then `retire` the old one."""
    if kind not in CREDENTIAL_KINDS:
        raise ValueError(f"a credential's kind is one of {', '.join(CREDENTIAL_KINDS)}")
    if origin not in CREDENTIAL_ORIGINS:
        raise ValueError(f"a credential's origin is one of {', '.join(CREDENTIAL_ORIGINS)}")
    if not value or len(value) > VALUE_MAX:
        raise ValueError(f"a credential is 1 to {VALUE_MAX} characters")
    if value != value.strip():
        raise ValueError("a credential has no spaces or line breaks around it")
    if valid_until is not None and valid_until.utcoffset() is None:
        raise ValueError("valid_until must be timezone-aware")
    if kind == "whsec":
        _key(value)
    elif kind == "whpk":
        public_key(value)
        has = conn.scalar(select(t.connections.c.endpoint_id).where(t.connections.c.name == name))
        if not has:
            raise ValueError("v2a needs the connection's endpoint id")
    return _add(conn, t, keyring, name, kind=kind, origin=origin, value=value, by=by,
                valid_until=valid_until)  # fmt: skip


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
    return add_credential(conn, t, keyring, name, "whsec", secret, by=by)


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
    return add_credential(conn, t, keyring, name, "whpk", key, by=by)


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
    """What the connection may prove its requests to us with now (what `Bridge` verifies with):
    its live shared secrets (`whsec`) and public keys (`whpk`), nothing else: a `token` or an
    `hmac` never verifies a request. None if it is unknown or off."""
    c, cc = t.connections, t.connection_credentials
    row = conn.execute(select(c.c.status, c.c.endpoint_id).where(c.c.name == name)).first()
    if row is None or row.status != "on":
        return None
    rows = conn.execute(
        select(cc.c.id, cc.c.kind, cc.c.sealed).where(
            cc.c.connection == name, cc.c.kind.in_(INBOUND_CREDENTIAL_KINDS), _live(cc)
        )
    ).all()
    found: dict[str, list[str]] = {"whsec": [], "whpk": []}
    for r in rows:
        found[r.kind].append(keyring.open(r.sealed, aad=_aad(r.id)).decode())
    return Credentials(tuple(found["whsec"]), tuple(found["whpk"]), row.endpoint_id)


def value(conn: Connection, t: RuntimeTables, keyring: Keyring, name: str, kind: str) -> str | None:
    """The newest live credential of `kind` on the connection, opened: the `token` to call them
    with, the `hmac` secret to check what they signed. None if it has none, or if the connection
    is unknown or off (off stops both ways: their requests get 404 and this gives nothing, so
    nothing is sent to them or taken from them). Reading it is not audited here: the app
    presents or checks it, it does not show it."""
    if kind not in CREDENTIAL_KINDS:
        raise ValueError(f"a credential's kind is one of {', '.join(CREDENTIAL_KINDS)}")
    c, cc = t.connections, t.connection_credentials
    row = conn.execute(
        select(cc.c.id, cc.c.sealed)
        .join(c, c.c.name == cc.c.connection)
        .where(cc.c.connection == name, cc.c.kind == kind, c.c.status == "on", _live(cc))
        .order_by(cc.c.created_at.desc(), cc.c.id.desc())
        .limit(1)
    ).first()
    return keyring.open(row.sealed, aad=_aad(row.id)).decode() if row else None


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
    "VALUE_MAX", "CredentialInfo", "add_credential", "add_public_key", "add_secret", "credentials",
    "ensure", "fingerprint", "generate_secret", "listing", "reseal", "retire", "reveal", "rotate",
    "set_status", "value",
]  # fmt: skip
