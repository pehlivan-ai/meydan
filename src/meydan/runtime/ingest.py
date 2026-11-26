"""What happens to a message between arriving and its turn, as subscribers of meydan.events.

A message is *ingested* once its content is in the system: text at once (in the receiving
transaction), a photo once it is downloaded and kept (`Ingest`, after the webhook answered). Only
then is it *seen* (`Seen`: the channel's read receipt, `Presence.read_after_s` after it arrived)
and only then does it join a turn (the worker waits up to `RuntimeConfig.ingest_wait_s`).

    message.received  ──Ingest──►  inbox.media, inbox.ingested_at  ──►  message.ingested
    message.ingested  ──Seen────►  channel.mark_read, inbox.read_at

Neither ever waits on a row a turn holds (the turn's transaction is open while the model thinks):
they skip a locked row and leave it to the turn, which marks read itself.
"""

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, func, select, update

from meydan.channels.base import Address, Inbound, seen_kind
from meydan.events import Event, Later, PermanentError, Subscriber, emit

if TYPE_CHECKING:
    from meydan.runtime.core import Runtime

log = logging.getLogger("meydan.runtime")

RECEIVED = "message.received"  # in the inbox; its content still to be fetched
INGESTED = "message.ingested"  # its content is in the system


def needs_fetch(rt: "Runtime", m: Inbound) -> bool:
    return (
        seen_kind(m.kind, m.payload) in rt.cfg.fetch_media
        and getattr(rt.channel, "download_media", None) is not None
    )


def arrived(rt: "Runtime", conn: Connection, m: Inbound, inbox_id: uuid.UUID) -> None:
    """In the receiving transaction: a message with nothing to fetch is ingested now; one with a
    file waits for `Ingest`."""
    if needs_fetch(rt, m):
        emit(conn, rt.t, RECEIVED, key=str(inbox_id), subject_id=inbox_id, channel=m.channel)
        return
    i = rt.t.inbox
    conn.execute(update(i).where(i.c.id == inbox_id).values(ingested_at=func.clock_timestamp()))
    emit(conn, rt.t, INGESTED, key=str(inbox_id), subject_id=inbox_id, channel=m.channel)


def _row(rt: "Runtime", inbox_id: uuid.UUID) -> dict[str, Any] | None:
    i, ident, cv = rt.t.inbox, rt.t.identities, rt.t.conversations
    ep = ident.alias("endpoint")
    with rt.engine.connect() as conn:
        r = (
            conn.execute(
                select(
                    i,
                    ident.c.scheme.label("sender_scheme"),
                    ident.c.scope.label("sender_scope"),
                    ep.c.scheme.label("to_scheme"),
                    ep.c.scope.label("to_scope"),
                )
                .join(ident, ident.c.id == i.c.sender_identity_id)
                .join(cv, cv.c.id == i.c.conversation_id)
                .join(ep, ep.c.id == cv.c.endpoint_identity_id)
                .where(i.c.id == inbox_id)
            )
            .mappings()
            .first()
        )
    return dict(r) if r else None


def _inbound(r: dict[str, Any]) -> Inbound:
    return Inbound(
        channel=r["channel"], message_id=r["message_id"],
        to=Address(r["to_scheme"], r["to"], r["to_scope"]),
        sender=Address(r["sender_scheme"], r["sender"], r["sender_scope"]),
        sent_at=r["sent_at"], kind=r["kind"], text=r["text"], payload=r["payload"] or {},
        sender_name=r["sender_name"], reply_to=r["reply_to"], media=r.get("media"),
    )  # fmt: skip


def _set_free(rt: "Runtime", conn: Connection, inbox_id: uuid.UUID, **values: Any) -> bool:
    """Update the inbox row unless a turn holds it (SKIP LOCKED): never wait on a turn."""
    i = rt.t.inbox
    free = select(i.c.id).where(i.c.id == inbox_id).with_for_update(skip_locked=True)
    done = conn.execute(update(i).where(i.c.id.in_(free.scalar_subquery())).values(**values))
    return bool(done.rowcount)


def ingest(rt: "Runtime") -> Subscriber:
    """Fetch and keep a message's file (a photo), then mark it ingested. Retried with backoff;
    the worker doesn't wait for it longer than `ingest_wait_s`."""

    def keep(data: bytes, mime: str) -> str:
        from meydan import media  # noqa: PLC0415

        with rt.engine.begin() as own:  # the file, committed whatever happens next
            return media.receive(own, rt.t, data, mime, store=rt.media_store,
                                 max_edge=rt.cfg.image_max_edge)  # fmt: skip

    def done(r: dict[str, Any], key: str | None) -> bool:
        with rt.engine.begin() as conn:
            if not _set_free(rt, conn, r["id"], media=key, ingested_at=func.clock_timestamp()):
                return False
            emit(conn, rt.t, INGESTED, key=str(r["id"]), subject_id=r["id"], channel=r["channel"])
            return True

    async def handle(event: Event) -> Later | None:
        r = await asyncio.to_thread(_row, rt, event.subject_id) if event.subject_id else None
        if r is None:
            raise PermanentError("no such message")
        if r["ingested_at"] is not None:
            return None  # done before (at least once: this may be a repeat)
        key = r["media"]
        if key is None:
            got = await rt.channel.download_media(_inbound(r))
            if got is not None:
                key = await asyncio.to_thread(keep, *got)
        if not await asyncio.to_thread(done, r, key):
            return Later(5)  # a turn has it (it went ahead without waiting): try again
        return None

    return Subscriber(
        f"meydan.ingest:{rt.serves or 'any'}", (RECEIVED,), handle, channel=rt.serves,
        lease_s=60,
    )  # fmt: skip


def seen(rt: "Runtime") -> Subscriber:
    """The channel's read receipt for an ingested message, `read_after_s` after it arrived, unless
    it was already read or answered. A refusal is recorded and tried again."""

    def stamp(inbox_id: uuid.UUID) -> None:
        with rt.engine.begin() as conn:
            _set_free(rt, conn, inbox_id, read_at=func.clock_timestamp())  # a turn stamps its own

    async def handle(event: Event) -> Later | None:
        r = await asyncio.to_thread(_row, rt, event.subject_id) if event.subject_id else None
        if r is None:
            raise PermanentError("no such message")
        if r["read_at"] is not None or r["processed_at"] is not None:
            return None  # answered (typing marked it read) or seen already
        await rt.channel.mark_read(r["to"], r["message_id"])
        await asyncio.to_thread(stamp, r["id"])
        return None

    return Subscriber(
        f"meydan.seen:{rt.serves or 'any'}", (INGESTED,), handle, channel=rt.serves,
        delay_s=rt.presence.read_after_s or 0.0, max_attempts=5, lease_s=30,
    )  # fmt: skip


def builtin(rt: "Runtime") -> list[Subscriber]:
    """The runtime's own subscribers, as its config and channel allow."""
    subs = []
    if rt.cfg.fetch_media and getattr(rt.channel, "download_media", None) is not None:
        subs.append(ingest(rt))
    if rt.presence.read_after_s is not None and getattr(rt.channel, "mark_read", None) is not None:
        subs.append(seen(rt))
    return subs
