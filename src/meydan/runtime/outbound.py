"""Sending: queue a turn's messages (paced like typing), then deliver them in order per
conversation, holding templates that aren't sendable, retrying with backoff, dead-lettering."""

import asyncio
import logging
import random
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, func, insert, literal, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from meydan.channels.base import Outbound, SendError
from meydan.runtime import templates
from meydan.runtime.types import _clock, _Part

log = logging.getLogger("meydan.runtime")


def backoff(attempt: int, min_wait_s: float = 0) -> float:
    # Full jitter on 2^n, capped at a minute; channel floors (e.g. WhatsApp pair rate
    # limit) grow as min_wait * 4^(n-1), as Meta recommends.
    jittered = random.uniform(0, min(60.0, 2.0**attempt))
    floor = min_wait_s * 4 ** (attempt - 1) if min_wait_s else 0
    return max(jittered, floor, 0.5)


AUTHORS = ("agent", "staff", "system")
# How long a channel's id for an uploaded file is used (WhatsApp keeps uploads 30 days).
UPLOAD_TTL = timedelta(days=29)


def queue_fallback(conn: Connection, t: Any, row: dict[str, Any]) -> bool:
    """Queue a message's fallback in its place, as the next thing in its conversation, and forget
    it on the row so it is queued once. False: the message has none."""
    message = row["message"] or {}
    fallback = message.get("fallback")
    if not fallback:
        return False
    o = t.outbox
    conn.execute(
        insert(o).values(
            channel=row["channel"], conversation_id=row["conversation_id"],
            recipient_identity_id=row["recipient_identity_id"], from_number=row["from_number"],
            to=row["to"], turn_id=row["turn_id"], seq=row["seq"], message=fallback,
            ref=fallback.get("ref"), author=row["author"], author_actor_id=row["author_actor_id"],
            created_at=_clock(), next_attempt_at=_clock(),
        )
    )  # fmt: skip
    forget_fallback(conn, t, o.c.id == row["id"])
    return True


def forget_fallback(conn: Connection, t: Any, where: Any) -> None:
    """Drop the fallback kept on rows: it is a copy of what the message says (a timetable's names,
    say), kept only while the message might still fail."""
    o = t.outbox
    conn.execute(
        update(o)
        .where(where, o.c.message.has_key("fallback"))
        .values(message=o.c.message.op("-")(literal("fallback")))
    )


class Outbox(_Part):
    def enqueue(
        self,
        conn: Connection,
        *,
        channel: str,
        conversation_id: uuid.UUID,
        from_number: str,
        to: str,
        messages: Sequence[Outbound],
        recipient_identity_id: uuid.UUID | None = None,
        start_at: datetime | None = None,
        turn_id: uuid.UUID | None = None,
        author: str = "system",
        author_actor_id: uuid.UUID | None = None,
    ) -> uuid.UUID | None:
        """Queue one outbound turn (one or more messages), paced like someone typing them.

        `author`: who wrote them, kept on each row (agent | staff | system); staff names the
        person (`author_actor_id`). Returns the turn id. Without `start_at` the turn starts now on
        the database clock."""
        if not messages:
            return None
        if author not in AUTHORS:
            raise ValueError(f"author must be one of {AUTHORS}")
        if (author == "staff") != (author_actor_id is not None):
            raise ValueError("a staff message names its author, and only a staff message does")
        turn_id = turn_id or uuid.uuid4()
        offset = timedelta()
        rows = []
        for seq, m in enumerate(messages):
            if seq:
                gap = len(m.text or "") / self.cfg.chars_per_second
                offset += timedelta(seconds=min(gap, self.cfg.max_bubble_gap_s))
            rows.append(
                {
                    "channel": channel,
                    "conversation_id": conversation_id,
                    "recipient_identity_id": recipient_identity_id,
                    "from_number": from_number,
                    "to": to,
                    "turn_id": turn_id,
                    "seq": seq,
                    "message": m.to_payload(),
                    "ref": m.ref,
                    "author": author,
                    "author_actor_id": author_actor_id,
                    "created_at": _clock(),
                    "next_attempt_at": (start_at + offset) if start_at else _clock() + offset,
                }
            )
        for row in rows:  # one statement per row: `next_attempt_at` may be a SQL expression
            conn.execute(insert(self.t.outbox).values(row))
        return turn_id

    def _lock_next(self, conn: Connection) -> dict[str, Any] | None:
        # The head of each conversation's queue only, so bubbles go out in order; SKIP LOCKED
        # lets several senders work on different conversations at once.
        row = (
            conn.execute(
                text(
                    f"""
                SELECT o.* FROM {self.t.outbox.name} o
                JOIN (SELECT DISTINCT ON (conversation_id) id FROM {self.t.outbox.name}
                      WHERE status = 'pending'
                        AND (CAST(:serves AS text) IS NULL OR channel = :serves)
                      ORDER BY conversation_id, id) head
                  ON head.id = o.id
                WHERE o.next_attempt_at <= now()
                ORDER BY o.next_attempt_at
                LIMIT 1
                FOR UPDATE OF o SKIP LOCKED
                """
                ),
                {"serves": self.serves},  # only this runtime's channel's replies
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def _settle(self, conn: Connection, row: dict[str, Any], values: dict[str, Any]) -> None:
        """A message that died goes out as its fallback, if it has one. One that left on a channel
        that reports no deliveries (Telegram) can't be known to fail later: its fallback is
        forgotten now; elsewhere a delivery receipt forgets it (inbound)."""
        status = values.get("status")
        if status == "dead":
            if queue_fallback(conn, self.t, row) and "message" in values:
                values["message"] = {k: v for k, v in values["message"].items() if k != "fallback"}
        elif status == "sent" and "delivered" not in self.rt.rules.of(row["channel"]).receipts:
            forget_fallback(conn, self.t, self.t.outbox.c.id == row["id"])
            if "message" in values:
                values["message"] = {k: v for k, v in values["message"].items() if k != "fallback"}

    def _save_attempt(self, conn: Connection, row_id: Any, values: dict[str, Any]) -> None:
        o = self.t.outbox
        conn.execute(update(o).where(o.c.id == row_id).values(**values))
        conn.commit()

    async def send_once(self) -> int:
        """Send due messages (head of each conversation). Returns how many were attempted."""
        attempted = 0
        while attempted < self.cfg.batch:
            conn = await asyncio.to_thread(self.engine.connect)
            try:
                conn.begin()
                row = await asyncio.to_thread(self._lock_next, conn)
                if row is None:
                    conn.rollback()
                    return attempted
                attempted += 1
                values = await asyncio.to_thread(self._template_check, conn, row)
                if values is None:
                    values = await self._attempt(row)
                await asyncio.to_thread(self._settle, conn, row, values)
                await asyncio.to_thread(self._save_attempt, conn, row["id"], values)
            finally:
                await asyncio.to_thread(conn.close)
        return attempted

    def _template_check(self, conn: Connection, row: dict[str, Any]) -> dict[str, Any] | None:
        """None: go ahead. Otherwise the row's new state: held out of the line until its
        template is sendable again, or dead if the template won't come back."""
        message = Outbound.from_payload(row["message"])
        if message.kind != "template":
            return None
        verdict, why = templates.verdict(conn, self.t, row["channel"], message)
        if verdict == "send":
            return None
        if verdict == "fail":
            log.warning("dead letter %s: %s", row["id"], why)
        status = "held" if verdict == "hold" else "dead"
        return {"status": status, "last_error": why, "updated_at": _clock()}

    async def _attempt(self, row: dict[str, Any]) -> dict[str, Any]:
        attempts = row["attempts"] + 1
        base = {"attempts": attempts, "updated_at": _clock()}
        try:
            message = Outbound.from_payload(row["message"])
            if message.media:  # an image, or a template's image header
                message = await self._with_media(row, message)
            wamid = await self.channel.send(row["from_number"], row["to"], message)
        except SendError as e:
            if not e.retryable or attempts >= self.cfg.max_send_attempts:
                log.warning("dead letter %s: %s", row["id"], e.reason)
                dead = base | {"status": "dead", "last_error": e.reason}
                if row["message"].get("otp"):
                    dead["message"] = {**row["message"], "otp": "******"}
                return dead
            wait = self.rt._backoff(attempts, e.min_wait_s)
            return base | {
                "last_error": e.reason,
                "next_attempt_at": _clock() + timedelta(seconds=wait),
            }
        sent = base | {
            "status": "sent",
            "channel_message_id": wamid,
            "sent_at": _clock(),
            "last_error": None,
        }
        if row["message"].get("otp"):  # a one-time code isn't kept once it has left
            sent["message"] = {**row["message"], "otp": "******"}
        return sent

    async def _with_media(self, row: dict[str, Any], message: Outbound) -> Outbound:
        """The image as the channel takes it: an id for a file it already has (uploaded once per
        sending number, reused while Meta keeps it), or else the file itself."""
        from meydan import media  # here: meydan.media imports the runtime's tables

        u, key, store = self.t.media_uploads, message.media, self.rt.media_store
        where = (u.c.media_key == key, u.c.channel == row["channel"],
                 u.c.from_number == row["from_number"])  # fmt: skip

        def cached() -> str | None:
            with self.engine.connect() as c:
                return c.scalar(
                    select(u.c.channel_media_id).where(
                        *where, u.c.uploaded_at > func.now() - UPLOAD_TTL
                    )
                )

        def read() -> tuple[bytes, str]:
            with self.engine.connect() as c:
                return media.load(c, self.t, key, store=store)

        def remember(media_id: str) -> None:
            with self.engine.begin() as c:
                c.execute(
                    pg_insert(u)
                    .values(media_key=key, channel=row["channel"], from_number=row["from_number"],
                            channel_media_id=media_id)
                    .on_conflict_do_update(
                        index_elements=[u.c.media_key, u.c.channel, u.c.from_number],
                        set_={"channel_media_id": media_id, "uploaded_at": func.now()},
                    )
                )  # fmt: skip

        try:
            upload = getattr(self.channel, "upload", None)
            if upload is None:  # a channel that takes the file with the message
                data, mime = await asyncio.to_thread(read)
                return replace(message, media_bytes=data, media_mime=mime)
            media_id = await asyncio.to_thread(cached)
            if media_id is None:
                data, mime = await asyncio.to_thread(read)
                media_id = await upload(row["from_number"], data, mime)
                await asyncio.to_thread(remember, media_id)
            return replace(message, channel_media_id=media_id)
        except media.MediaMissingError as e:
            raise SendError(f"image: no file {e}", retryable=False) from None
