"""Receiving: new messages (identity settled, duplicates ignored), interceptors, delivery
receipts (and who they name), template updates; and what an answer points at."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Connection, case, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from meydan.channels.base import Inbound, StatusUpdate, TemplateUpdate
from meydan.runtime import identity, templates
from meydan.runtime.tables import RuntimeTables
from meydan.runtime.types import _STATUS_RANK, Intercepted, _clock, _Part  # noqa: F401 (Intercepted: re-export)


@dataclass(frozen=True)
class Replied:
    """What an incoming message answers: one of our messages, and the app's key for it."""

    outbox_id: uuid.UUID
    ref: str | None
    turn_id: uuid.UUID | None


def replied_to(conn: Connection, t: RuntimeTables, message: Inbound) -> Replied | None:
    """The message of ours that `message` answers (a button press, a quoted reply), if the
    channel said so and we sent it. For interceptors: match the answer by what it answers."""
    if not message.reply_to:
        return None
    o = t.outbox
    row = conn.execute(
        select(o.c.id, o.c.ref, o.c.turn_id).where(
            o.c.channel == message.channel, o.c.channel_message_id == message.reply_to
        )
    ).first()
    return Replied(row.id, row.ref, row.turn_id) if row else None


class Receiver(_Part):
    """Call from the webhook, inside the request's transaction."""

    def receive(
        self,
        conn: Connection,
        messages: Sequence[Inbound],
        statuses: Sequence[StatusUpdate],
        template_updates: Sequence[TemplateUpdate] = (),
    ) -> int:
        """Store new messages (duplicates are ignored), apply delivery receipts (and what they say
        about who received them: `identity.corroborate`) and what the channel says about our
        templates (held messages are released or given up).

        Returns how many messages were new. Cheap: safe to call before answering 200.
        """
        new = 0
        i = self.t.inbox
        for m in messages:
            seen = select(i.c.id).where(i.c.channel == m.channel, i.c.message_id == m.message_id)
            if conn.scalar(seen) is not None:
                continue  # duplicate delivery: identities and routing were settled the first time
            routed = identity.route(
                conn,
                self.t,
                channel=m.channel,
                to=m.to,
                sender=m.sender,
                also=m.also,
                name=m.sender_name,
                attrs=m.attrs,
                evidence={"message_id": m.message_id},
            )
            row = {
                "channel": m.channel,
                "message_id": m.message_id,
                "conversation_id": routed.conversation_id,
                "sender_identity_id": routed.sender_identity_id,
                "to": m.to.value,
                "sender": m.sender.value,
                "sender_name": m.sender_name,
                "kind": m.kind,
                "text": m.text,
                "payload": m.payload,
                "reply_to": m.reply_to,
                "sent_at": m.sent_at,
            }
            stmt = (
                pg_insert(self.t.inbox)
                .values(row)
                .on_conflict_do_nothing()
                .returning(self.t.inbox.c.id)
            )
            inserted = conn.execute(stmt).all()  # rowcount is unreliable with ON CONFLICT
            new += len(inserted)
            if inserted:
                self._intercept(conn, m, routed, inserted[0][0])
        for s in statuses:
            self._apply_status(conn, s)
            if len(s.recipient) > 1:  # phone + BSUID in one signed receipt: the same person
                identity.corroborate(
                    conn, self.t, list(s.recipient), evidence={"status_of": s.message_id}
                )
        templates.record_all(conn, self.t, template_updates)
        return new

    def _intercept(
        self, conn: Connection, m: Inbound, routed: "identity.Routed", inbox_id: uuid.UUID
    ) -> None:
        """Let interceptors consume a new message before any turn (first one wins)."""
        for intercept in self.intercept:
            result = intercept(conn, m, routed)
            if result is None:
                continue
            i = self.t.inbox
            conn.execute(
                update(i).where(i.c.id == inbox_id).values(processed_at=_clock(), read_at=_clock())
            )
            self.enqueue(
                conn,
                channel=m.channel,
                conversation_id=routed.conversation_id,
                from_number=m.to.value,
                to=m.sender.value,
                recipient_identity_id=routed.sender_identity_id,
                messages=result.replies,
            )
            return

    def _apply_status(self, conn: Connection, s: StatusUpdate) -> None:
        o = self.t.outbox
        cond = o.c.channel_message_id == s.message_id
        if s.status == "failed":
            cond &= o.c.status != "read"
        elif s.status in _STATUS_RANK:
            # Receipts arrive unordered: only move forward (sent -> delivered -> read).
            cond &= case(_STATUS_RANK, value=o.c.status, else_=99) < _STATUS_RANK[s.status]
        else:
            return
        conn.execute(
            update(o).where(cond).values(status=s.status, last_error=s.error, updated_at=_clock())
        )
        # Receipt times, whatever order receipts arrive in: a read implies it was delivered.
        mine = o.c.channel_message_id == s.message_id
        if s.status in ("delivered", "read"):
            conn.execute(
                update(o).where(mine, o.c.delivered_at.is_(None)).values(delivered_at=s.at)
            )
        if s.status == "read":
            conn.execute(update(o).where(mine, o.c.read_at.is_(None)).values(read_at=s.at))
