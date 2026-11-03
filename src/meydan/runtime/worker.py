"""Turns: "seen" shortly after a message arrives, then one turn per conversation at a time
(debounced, resequenced, single writer), with "typing…", the app's handler, and failures
recorded (poison messages given up after a few attempts)."""

import asyncio
import logging
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, func, select, update
from sqlalchemy.dialects.postgresql import distinct_on

from meydan.channels.base import Address, Inbound, Outbound
from meydan.observe.context import TurnContext, turn_scope
from meydan.observe.record import write_turn
from meydan.runtime.types import Turn, _clock, _Part

log = logging.getLogger("meydan.runtime")


class Worker(_Part):
    def _due_reads(self) -> list[dict[str, Any]]:
        after = self.presence.read_after_s
        if after is None:
            return []
        i = self.t.inbox
        # The latest unread message of each conversation; marking it read covers earlier ones.
        q = (
            select(i.c.conversation_id, i.c.to, i.c.message_id, i.c.received_at)
            .where(
                i.c.read_at.is_(None),
                i.c.processed_at.is_(None),  # answered messages need no separate "seen"
                i.c.received_at <= _clock() - timedelta(seconds=after),
            )
            .order_by(i.c.conversation_id, i.c.sent_at.desc(), i.c.id.desc())
            .ext(distinct_on(i.c.conversation_id))
            .limit(self.cfg.batch)
        )
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    def _set_read(self, conversation_id: uuid.UUID, up_to: datetime) -> None:
        i = self.t.inbox
        with self.engine.begin() as conn:
            conn.execute(
                update(i)
                .where(
                    i.c.conversation_id == conversation_id,
                    i.c.read_at.is_(None),
                    i.c.received_at <= up_to,
                )
                .values(read_at=_clock())
            )

    async def read_once(self) -> int:
        """Send due read receipts (presence.read_after_s). Returns how many conversations."""
        due = await asyncio.to_thread(self._due_reads)
        mark_read = getattr(self.channel, "mark_read", None)
        for row in due:
            if mark_read is not None:
                try:  # cosmetic: never let it break anything
                    await mark_read(row["to"], row["message_id"])
                except Exception:
                    log.warning("read receipt failed for %s", row["conversation_id"], exc_info=True)
            await asyncio.to_thread(self._set_read, row["conversation_id"], row["received_at"])
        return len(due)

    def _due_conversations(self) -> list[uuid.UUID]:
        i = self.t.inbox
        now = _clock()
        q = (
            select(i.c.conversation_id)
            .where(i.c.processed_at.is_(None))
            .group_by(i.c.conversation_id)
            .having(
                (func.max(i.c.received_at) <= now - timedelta(seconds=self.cfg.debounce_s))
                | (func.min(i.c.received_at) <= now - timedelta(seconds=self.cfg.max_wait_s))
            )
            .order_by(func.min(i.c.received_at))
            .limit(self.cfg.batch)
        )
        with self.engine.connect() as conn:
            return list(conn.scalars(q))

    async def work_once(self) -> int:
        """Run at most one turn for each conversation that's due. Returns turns run."""
        keys = await asyncio.to_thread(self._due_conversations)
        done = 0
        for key in keys:
            done += await self._turn(key)
        return done

    def _claim(self, conn: Connection, key: uuid.UUID) -> list[dict[str, Any]] | None:
        lock = func.pg_try_advisory_xact_lock(func.hashtextextended(f"conversation:{key}", 0))
        if not conn.scalar(select(lock)):
            return None  # another worker is in a turn for this conversation
        i, ident, cv, cl = self.t.inbox, self.t.identities, self.t.conversations, self.t.claims
        ep = ident.alias("endpoint")
        rows = conn.execute(
            select(
                i,
                ident.c.scheme.label("sender_scheme"),
                ident.c.scope.label("sender_scope"),
                ep.c.scheme.label("to_scheme"),
                ep.c.scope.label("to_scope"),
                cl.c.actor_id,
                # Seconds since "seen", on the database clock (None: not read yet).
                func.extract("epoch", _clock() - i.c.read_at).label("since_read"),
            )
            .join(ident, ident.c.id == i.c.sender_identity_id)
            .join(cv, cv.c.id == i.c.conversation_id)
            .join(ep, ep.c.id == cv.c.endpoint_identity_id)
            .outerjoin(
                cl,
                (cl.c.identity_id == i.c.sender_identity_id)
                & (cl.c.state == "active")
                & cl.c.valid_to.is_(None),
            )
            .where(i.c.conversation_id == key, i.c.processed_at.is_(None))
            .order_by(i.c.sent_at, i.c.id)  # Resequencer: the sender's order, not arrival
        ).mappings()
        return [dict(r) for r in rows]

    async def _turn(self, key: uuid.UUID) -> int:
        conn = await asyncio.to_thread(self.engine.connect)
        try:
            trans = conn.begin()
            rows = await asyncio.to_thread(self._claim, conn, key)
            if not rows:
                trans.rollback()
                return 0
            turn = _turn_from_rows(key, rows)
            ids = [r["id"] for r in rows]
            last = rows[-1]["message_id"]
            started_at = await asyncio.to_thread(conn.scalar, select(_clock()))
            ctx = TurnContext(uuid.uuid4(), key, turn.actor_id)
            refresh = None
            if self.presence.typing:
                await self._read_then_wait(conn, turn, rows)
                await self._typing(turn.to, last, key)
                await asyncio.to_thread(self._stamp, conn, ids, "typing_at")
                if self.presence.typing_refresh_s:
                    refresh = asyncio.create_task(self._keep_typing(turn.to, last, key))
            try:
                with turn_scope(ctx):
                    replies = await self.handler(turn)
            except Exception as e:
                trans.rollback()
                log.exception("turn failed for %s", key)
                await asyncio.to_thread(self._record_failure, turn, ids, e, ctx, started_at)
                return 0
            finally:
                if refresh is not None:
                    refresh.cancel()

            def commit() -> None:
                self.enqueue(
                    conn,
                    channel=turn.channel,
                    conversation_id=key,
                    from_number=turn.to,
                    to=turn.sender.value,
                    recipient_identity_id=turn.sender_identity_id,
                    messages=replies,
                    turn_id=ctx.turn_id,
                )
                self._mark_processed(conn, ids)
                write_turn(
                    conn,
                    self.t,
                    ctx,
                    channel=turn.channel,
                    started_at=started_at,
                    status="replied",
                    messages_in=len(ids),
                    messages_out=len(replies),
                )
                trans.commit()

            await asyncio.to_thread(commit)
            return 1
        finally:
            await asyncio.to_thread(conn.close)

    async def _read_then_wait(
        self, conn: Connection, turn: Turn, rows: list[dict[str, Any]]
    ) -> None:
        """Make sure the person has seen "seen" before "typing…", with the configured gap."""
        since = rows[-1]["since_read"]
        if since is None:  # the reader hasn't marked it yet (or read receipts are off)
            mark_read = getattr(self.channel, "mark_read", None)
            if mark_read is not None and self.presence.read_after_s is not None:
                try:
                    await mark_read(turn.to, rows[-1]["message_id"])
                except Exception:
                    log.warning("read receipt failed for %s", turn.conversation_id, exc_info=True)
                await asyncio.to_thread(self._stamp, conn, [r["id"] for r in rows], "read_at")
                since = 0.0
            else:
                return  # typing alone also marks read on WhatsApp; nothing to wait for
        wait = self.presence.typing_after_read_s - float(since)
        if wait > 0:
            await asyncio.sleep(wait)

    async def _typing(self, to: str, message_id: str, key: uuid.UUID) -> None:
        try:  # cosmetic: never let it break a turn
            await self.channel.typing(to, message_id)
        except Exception:
            log.warning("typing indicator failed for %s", key, exc_info=True)

    async def _keep_typing(self, to: str, message_id: str, key: uuid.UUID) -> None:
        """Re-send "typing…" while a slow turn is still running (channels drop it after a while)."""
        every = self.presence.typing_refresh_s or 0
        while every > 0:
            await asyncio.sleep(every)
            await self._typing(to, message_id, key)

    def _stamp(self, conn: Connection, ids: list, column: str) -> None:
        i = self.t.inbox
        conn.execute(
            update(i).where(i.c.id.in_(ids), i.c[column].is_(None)).values({column: _clock()})
        )

    def _mark_processed(self, conn: Connection, ids: list, error: str | None = None) -> None:
        i = self.t.inbox
        conn.execute(update(i).where(i.c.id.in_(ids)).values(processed_at=_clock(), error=error))

    def _record_failure(
        self,
        turn: Turn,
        ids: list,
        exc: BaseException,
        ctx: TurnContext,
        started_at: datetime,
    ) -> None:
        i = self.t.inbox
        with self.engine.begin() as conn:
            attempts = conn.scalars(
                update(i)
                .where(i.c.id.in_(ids))
                .values(attempts=i.c.attempts + 1, error=repr(exc)[:500])
                .returning(i.c.attempts)
            ).all()
            gave_up = bool(attempts) and max(attempts) >= self.cfg.max_turn_attempts
            replies: Sequence[Outbound] = []
            if gave_up:
                self._mark_processed(conn, ids, error=f"gave up: {exc!r}"[:500])
                if self.on_failure:
                    replies = self.on_failure(turn, exc)
                    self.enqueue(
                        conn,
                        channel=turn.channel,
                        conversation_id=turn.conversation_id,
                        from_number=turn.to,
                        to=turn.sender.value,
                        recipient_identity_id=turn.sender_identity_id,
                        messages=replies,
                        turn_id=ctx.turn_id,
                    )
            # A failed turn is recorded too: the model calls it made still cost money.
            write_turn(
                conn,
                self.t,
                ctx,
                channel=turn.channel,
                started_at=started_at,
                status="gave_up" if gave_up else "failed",
                messages_in=len(ids),
                messages_out=len(replies),
                error=repr(exc),
            )


def _turn_from_rows(key: uuid.UUID, rows: list[dict[str, Any]]) -> Turn:
    msgs = [
        Inbound(
            channel=r["channel"],
            message_id=r["message_id"],
            to=Address(r["to_scheme"], r["to"], r["to_scope"]),
            sender=Address(r["sender_scheme"], r["sender"], r["sender_scope"]),
            sent_at=r["sent_at"],
            kind=r["kind"],
            text=r["text"],
            payload=r["payload"] or {},
            sender_name=r["sender_name"],
            reply_to=r["reply_to"],
        )
        for r in rows
    ]
    last = rows[-1]  # answer the address that wrote last (it may have changed mid-burst)
    name = next((r["sender_name"] for r in reversed(rows) if r["sender_name"]), None)
    return Turn(
        conversation_id=key,
        channel=last["channel"],
        to=last["to"],
        sender=msgs[-1].sender,
        sender_identity_id=last["sender_identity_id"],
        actor_id=last["actor_id"],
        sender_name=name,
        messages=msgs,
    )
