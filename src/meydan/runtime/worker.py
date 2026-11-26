"""Turns: "seen" shortly after a message arrives, then one turn per conversation at a time
(debounced, resequenced, single writer), with "typing…", the app's handler, and failures
recorded (poison messages given up after a few attempts)."""

import asyncio
import logging
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import distinct_on

from meydan.channels.base import Address, Inbound, Outbound
from meydan.observe.context import TurnContext, turn_scope
from meydan.observe.record import write_turn
from meydan.runtime.failures import Failure, classify
from meydan.runtime.tables import RuntimeTables
from meydan.runtime.types import Turn, _clock, _Part

log = logging.getLogger("meydan.runtime")


def taken_over_now(t: RuntimeTables, conversation_id):
    """A SQL condition: this conversation is taken over by a person now (meydan.operator)."""
    h = t.operator_holds
    return exists().where(
        h.c.conversation_id == conversation_id, (h.c.until.is_(None)) | (h.c.until > func.now())
    )


def _taken_over(conn: Connection, t: RuntimeTables, conversation_id: uuid.UUID) -> bool:
    return bool(conn.scalar(select(taken_over_now(t, conversation_id))))


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
                *self._ours(),
            )
            .order_by(i.c.conversation_id, i.c.sent_at.desc(), i.c.id.desc())
            .ext(distinct_on(i.c.conversation_id))
            .limit(self.cfg.batch)
        )
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    def _ours(self) -> list:
        """Only this runtime's channel's messages (`Runtime.serves`); every one without a name."""
        return [self.t.inbox.c.channel == self.serves] if self.serves else []

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
            .where(
                i.c.processed_at.is_(None),
                # A turn put off waits; a new message from the person tries it again at once.
                or_(i.c.retry_at.is_(None), i.c.retry_at <= now),
                # A conversation a person has taken over waits: its messages stay unprocessed and
                # the agent answers what is still unanswered once it is handed back (or ends).
                ~taken_over_now(self.t, i.c.conversation_id),
                *self._ours(),
            )
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

    async def _keep_media(self, conn: Connection, rows: list[dict[str, Any]]) -> None:
        """Keep the files in these messages (photos) before the turn, so the agent and the team
        can see them. A file that can't be fetched leaves the message as it is: the turn still
        runs, the agent reads "[image]" (logged; no address or content in the log)."""
        download = getattr(self.channel, "download_media", None)
        if download is None or not self.cfg.fetch_media:
            return
        for r in rows:
            if r["kind"] not in self.cfg.fetch_media or r.get("media"):
                continue
            try:
                got = await download(_inbound(r))
                if got is not None:
                    r["media"] = await asyncio.to_thread(self._store_media, conn, r["id"], *got)
            except Exception as e:  # noqa: BLE001 - the turn goes on without the file
                log.warning("media of inbox %s not kept: %s", r["id"], e)

    def _store_media(self, conn: Connection, inbox_id: uuid.UUID, data: bytes, mime: str) -> str:
        """The file in its own transaction, committed now: the agent's handler reads it on
        another connection while this turn's is still open. The inbox row points at it in the
        turn's transaction (a turn put back leaves the file; the next try finds it by its hash)."""
        from meydan import media  # noqa: PLC0415

        with self.engine.begin() as own:
            key = media.receive(
                own, self.t, data, mime, store=self.rt.media_store,
                max_edge=self.cfg.image_max_edge,
            )  # fmt: skip
        i = self.t.inbox
        conn.execute(update(i).where(i.c.id == inbox_id).values(media=key))
        return key

    async def _turn(self, key: uuid.UUID) -> int:
        conn = await asyncio.to_thread(self.engine.connect)
        try:
            trans = conn.begin()
            rows = await asyncio.to_thread(self._claim, conn, key)
            if not rows:
                trans.rollback()
                return 0
            ids = [r["id"] for r in rows]
            if await asyncio.to_thread(_taken_over, conn, self.t, key):
                trans.rollback()  # taken over meanwhile: left for after the handback
                return 0
            await self._keep_media(conn, rows)
            turn = _turn_from_rows(key, rows)
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

            def commit() -> bool:
                # Taken over while the agent was thinking: its reply does not go out, and the
                # messages wait for after the handback (checked in the reply's own transaction).
                if _taken_over(conn, self.t, key):
                    trans.rollback()
                    return False
                self.enqueue(
                    conn,
                    channel=turn.channel,
                    conversation_id=key,
                    from_number=turn.to,
                    to=turn.sender.value,
                    recipient_identity_id=turn.sender_identity_id,
                    messages=replies,
                    turn_id=ctx.turn_id,
                    author="agent",  # the turn's own reply
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
                return True

            return 1 if await asyncio.to_thread(commit) else 0
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
        """Count the attempt, then try again later, a little later, or give up, by what failed
        (`meydan.runtime.failures`). Only `retry` failures count toward `max_turn_attempts`: an
        hour of a provider being down is not a poison message."""
        i = self.t.inbox
        kind, group, retry_after = classify(exc)
        with self.engine.begin() as conn:
            attempts, first, now = conn.execute(
                select(func.max(i.c.attempts) + 1, func.min(i.c.received_at), _clock()).where(
                    i.c.id.in_(ids)
                )
            ).one()
            window = self.rules.of(turn.channel).reply_window
            horizon = first + (window or timedelta(seconds=self.cfg.give_up_after_s))
            waits = self.cfg.retry_waits_s
            if group == "transient":
                wait = max(waits[min(attempts, len(waits)) - 1], retry_after or 0)
                retry_at = now + timedelta(seconds=wait)
                gave_up = retry_at >= horizon
            elif group == "retry":
                retry_at = now + timedelta(seconds=min(self.cfg.retry_wait_s * attempts, 60))
                gave_up = attempts >= self.cfg.max_turn_attempts
            else:  # human, permanent
                retry_at, gave_up = None, True
            failure = Failure(kind, group, exc, attempts, gave_up, retry_after)
            error = f"{kind}: {exc!r}"[:500]
            conn.execute(
                update(i)
                .where(i.c.id.in_(ids))
                .values(attempts=attempts, error=error, retry_at=None if gave_up else retry_at)
            )
            replies: Sequence[Outbound] = []
            if gave_up:
                self._mark_processed(conn, ids, error=f"gave up, {error}"[:500])
                if self.on_failure:
                    replies = self.on_failure(turn, failure)
            elif attempts == 1 and self.on_delay:  # put off: said once, not at every try
                replies = self.on_delay(turn, failure)
            if replies:
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
                status="gave_up" if gave_up else "failed",  # put off or not: `error` says why
                messages_in=len(ids),
                messages_out=len(replies),
                error=error,
            )


def _inbound(r: dict[str, Any]) -> Inbound:
    return Inbound(
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
        media=r.get("media"),
    )


def _turn_from_rows(key: uuid.UUID, rows: list[dict[str, Any]]) -> Turn:
    msgs = [_inbound(r) for r in rows]
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
