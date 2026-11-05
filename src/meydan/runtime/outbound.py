"""Sending: queue a turn's messages (paced like typing), then deliver them in order per
conversation, holding templates that aren't sendable, retrying with backoff, dead-lettering."""

import asyncio
import logging
import random
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, insert, text, update

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
    ) -> uuid.UUID | None:
        """Queue one outbound turn (one or more messages), paced like someone typing them.

        Returns the turn id. Without `start_at` the turn starts now on the database clock."""
        if not messages:
            return None
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
                      WHERE status = 'pending' ORDER BY conversation_id, id) head
                  ON head.id = o.id
                WHERE o.next_attempt_at <= now()
                ORDER BY o.next_attempt_at
                LIMIT 1
                FOR UPDATE OF o SKIP LOCKED
                """
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

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
            wamid = await self.channel.send(
                row["from_number"], row["to"], Outbound.from_payload(row["message"])
            )
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
