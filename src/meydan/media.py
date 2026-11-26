"""Media: files kept once, by their SHA-256, in a store the app chooses: those we send, and those
people send us (`receive`, `forget_received`).

meydan keeps what it must know about each file in Postgres (`<prefix>media`: key, type, size,
which store has it) and what a channel gave it for a file (`<prefix>media_uploads`: WhatsApp's
media id per sending number, kept 30 days by Meta). Where the bytes live is the store's business:
`PostgresStore` (bytea, `<prefix>media_blobs`) is the default; an app can give the runtime
another `MediaStore` (object storage, say) without anything else changing.

Send a file: `key = media.put(conn, tables, data, "image/jpeg")`, then
`Outbound.image_message(key)`. The sender turns the key into what the channel needs when the
message leaves (an upload id, or the bytes).

Keep a file someone sent: the runtime's worker does it before the turn (the channel's
`download_media`, then `receive`): a photo is made ready for a model (`prepare_image`) and the
inbox row points at it. `forget_received` drops what is past the app's retention. See
docs/media.md.
"""

import hashlib
import io
import logging
from datetime import timedelta
from typing import Protocol

from sqlalchemy import Connection, exists, func, select, update
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


# --- files people send us ----------------------------------------------------------------------

log = logging.getLogger(__name__)

# The long edge a photo is brought down to before it is kept: what a model's standard tier reads
# without scaling it again (Anthropic: 1568 px; OpenAI "high": 2048 px). Cost follows pixels.
IMAGE_MAX_EDGE = 1568


def prepare_image(data: bytes, mime: str, max_edge: int = IMAGE_MAX_EDGE) -> tuple[bytes, str]:
    """A photo as a model should get it: turned upright (EXIF orientation), no larger than
    `max_edge` on its long side, JPEG, with its metadata (location, camera) left behind. Needs
    Pillow (extra `media`); without it, or for a file Pillow can't read, the file stays as it came
    (logged once)."""
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError  # noqa: PLC0415
    except ImportError:
        _warn_once("Pillow is not installed (meydan[media]): received photos are kept as sent")
        return data, mime
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((max_edge, max_edge))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            out = io.BytesIO()
            im.save(out, "JPEG", quality=85, optimize=True)  # no exif passed: metadata dropped
            return out.getvalue(), "image/jpeg"
    except (UnidentifiedImageError, OSError, ValueError):
        log.warning("a received image could not be read; kept as sent (%s)", mime)
        return data, mime


_warned: set[str] = set()


def _warn_once(text: str) -> None:
    if text not in _warned:
        _warned.add(text)
        log.warning(text)


def receive(
    conn: Connection, tables: RuntimeTables, data: bytes, mime: str, *,
    store: MediaStore | None = None, max_edge: int = IMAGE_MAX_EDGE,
) -> str:  # fmt: skip
    """Keep a file someone sent and return its key: a photo is prepared first (`prepare_image`)."""
    if mime.startswith("image/"):
        data, mime = prepare_image(data, mime, max_edge)
    return put(conn, tables, data, mime, store=store)


def forget_received(
    conn: Connection, tables: RuntimeTables, older_than: timedelta, *,
    store: MediaStore | None = None,
) -> int:  # fmt: skip
    """Drop the files people sent more than `older_than` ago: their inbox rows keep the text and
    lose the file. A file still used elsewhere (a newer message, one we sent, or the app's own
    reference to it, e.g. a poster) stays. Returns how many files were deleted."""
    i, o, m = tables.inbox, tables.outbox, tables.media
    store = store or PostgresStore(tables)
    old = (i.c.media.is_not(None)) & (i.c.received_at < func.now() - older_than)
    keys = set(conn.scalars(select(i.c.media).where(old).distinct()))
    if not keys:
        return 0
    conn.execute(update(i).where(old).values(media=None))
    unused = conn.scalars(
        select(m.c.key).where(
            m.c.key.in_(keys),
            ~exists().where(i.c.media == m.c.key),
            ~exists().where(o.c.message["media"].astext == m.c.key),
        )
    ).all()
    gone = 0
    for key in unused:
        try:
            with conn.begin_nested():  # the app's own reference (a foreign key) keeps the file
                conn.execute(m.delete().where(m.c.key == key))
                store.delete(conn, key)
        except Exception:  # noqa: BLE001 - a file still referenced is kept, on purpose
            continue
        gone += 1
    return gone
