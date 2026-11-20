"""Media we send: files kept once, by their SHA-256, in a store the app chooses.

meydan keeps what it must know about each file in Postgres (`<prefix>media`: key, type, size,
which store has it) and what a channel gave it for a file (`<prefix>media_uploads`: WhatsApp's
media id per sending number, kept 30 days by Meta). Where the bytes live is the store's business:
`PostgresStore` (bytea, `<prefix>media_blobs`) is the default; an app can give the runtime
another `MediaStore` (object storage, say) without anything else changing.

Send a file: `key = media.put(conn, tables, data, "image/jpeg")`, then
`Outbound.image_message(key)`. The sender turns the key into what the channel needs when the
message leaves (an upload id, or the bytes). See docs/media.md.
"""

import hashlib
from typing import Protocol

from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert

from meydan.runtime.tables import RuntimeTables


class MediaStore(Protocol):
    """Where a file's bytes live. Called with the caller's connection, so a database store
    writes in the same transaction; a store elsewhere may ignore it."""

    name: str  # recorded on each file, so a file is always read from the store that has it

    def put(self, conn: Connection, key: str, data: bytes, mime: str) -> None: ...

    def get(self, conn: Connection, key: str) -> bytes: ...

    def delete(self, conn: Connection, key: str) -> None: ...


class PostgresStore:
    """Bytes in Postgres (`<prefix>media_blobs`, bytea): nothing else to run, and the files are
    in the database's backups. Right for a few files; many or large ones belong elsewhere."""

    name = "postgres"

    def __init__(self, tables: RuntimeTables):
        self.t = tables

    def put(self, conn: Connection, key: str, data: bytes, mime: str) -> None:
        conn.execute(insert(self.t.media_blobs).values(key=key, data=data).on_conflict_do_nothing())

    def get(self, conn: Connection, key: str) -> bytes:
        data = conn.scalar(select(self.t.media_blobs.c.data).where(self.t.media_blobs.c.key == key))
        if data is None:
            raise MediaMissingError(key)
        return bytes(data)

    def delete(self, conn: Connection, key: str) -> None:
        conn.execute(self.t.media_blobs.delete().where(self.t.media_blobs.c.key == key))


class MediaMissingError(LookupError):
    """No file with this key (or its store no longer has the bytes)."""


def key_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def put(
    conn: Connection, tables: RuntimeTables, data: bytes, mime: str, *,
    store: MediaStore | None = None,
) -> str:  # fmt: skip
    """Keep a file and return its key (SHA-256, hex). The same bytes twice are one file."""
    if not data:
        raise ValueError("an empty file")
    if "/" not in mime:
        raise ValueError(f"not a media type: {mime!r}")
    store = store or PostgresStore(tables)
    key = key_of(data)
    m = tables.media
    new = conn.execute(
        insert(m)
        .values(key=key, mime=mime, size=len(data), store=store.name)
        .on_conflict_do_nothing()
        .returning(m.c.key)
    ).first()
    if new:
        store.put(conn, key, data, mime)
    return key


def load(
    conn: Connection, tables: RuntimeTables, key: str, *, store: MediaStore | None = None
) -> tuple[bytes, str]:
    """A file's bytes and media type. MediaMissingError if there is no such file, or the store given
    isn't the one that holds it."""
    m = tables.media
    row = conn.execute(select(m.c.mime, m.c.store).where(m.c.key == key)).first()
    if row is None:
        raise MediaMissingError(key)
    store = store or PostgresStore(tables)
    if row.store != store.name:
        raise MediaMissingError(f"{key} is in store {row.store!r}, not {store.name!r}")
    return store.get(conn, key), row.mime
