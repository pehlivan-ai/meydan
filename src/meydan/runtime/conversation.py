"""What was said in a conversation, both directions, on one clock."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, select

from meydan.channels.base import REACTION, describe
from meydan.runtime.tables import RuntimeTables


@dataclass(frozen=True)
class Said:
    """One line of a conversation's transcript: something received or sent."""

    at: datetime  # database clock: when we received it / queued it (one clock for both sides)
    direction: str  # "in" | "out"
    identity_id: uuid.UUID | None  # who said it (in) / to whom (out)
    text: str
    turn_id: uuid.UUID | None = None  # outbound: messages of one reply share it
    media: str | None = None  # inbound: the file they sent, kept in meydan.media (a photo)


def transcript(
    conn: Connection, t: RuntimeTables, conversation_id: uuid.UUID, limit: int = 30
) -> list[Said]:
    """The last `limit` things said in a conversation, oldest first.

    Inbound: messages already consumed by a turn, as `describe` reads them (a photo named, a
    reaction with the message it is on). Outbound: everything queued except dead letters. Both
    sides are ordered on the database clock: a sender's device clock (the channel timestamp) must
    not interleave with ours."""
    i, o = t.inbox, t.outbox
    ins = conn.execute(
        select(
            i.c.received_at,
            i.c.id,
            i.c.sender_identity_id,
            i.c.text,
            i.c.kind,
            i.c.reply_to,
            i.c.media,
        )  # fmt: skip
        .where(i.c.conversation_id == conversation_id, i.c.processed_at.is_not(None))
        .order_by(i.c.received_at.desc(), i.c.id.desc())
        .limit(limit)
    ).all()
    outs = conn.execute(
        select(o.c.created_at, o.c.id, o.c.recipient_identity_id, o.c.message, o.c.turn_id)
        .where(o.c.conversation_id == conversation_id, o.c.status != "dead")
        .order_by(o.c.created_at.desc(), o.c.id.desc())
        .limit(limit)
    ).all()
    on = _reacted_to(conn, t, [r.reply_to for r in ins if r.kind == REACTION and r.reply_to])
    lines = [
        (at, id_, Said(at, "in", who, describe(kind, text or "", on.get(reply_to)), media=media))
        for at, id_, who, text, kind, reply_to, media in ins
    ]
    lines += [
        # What an agent reads of our own message: its `recall` if it has one, else its text. The
        # row keeps the real message for the people allowed to see it (meydan.observe).
        (
            at,
            id_,
            Said(at, "out", who, (msg or {}).get("recall") or (msg or {}).get("text") or "", turn),
        )  # fmt: skip
        for at, id_, who, msg, turn in outs
    ]
    return [said for _, _, said in sorted(lines, key=lambda x: (x[0], x[1]))[-limit:]]


def _reacted_to(conn: Connection, t: RuntimeTables, ids: list[str]) -> dict[str, str]:
    """The text of the messages reactions are on, by channel message id: ours (what an agent
    reads of them: recall, else text) or theirs."""
    if not ids:
        return {}
    o, i = t.outbox, t.inbox
    found: dict[str, str] = {}
    for mid, msg in conn.execute(
        select(o.c.channel_message_id, o.c.message).where(o.c.channel_message_id.in_(ids))
    ):
        found[mid] = (msg or {}).get("recall") or (msg or {}).get("text") or ""
    for mid, text in conn.execute(select(i.c.message_id, i.c.text).where(i.c.message_id.in_(ids))):
        found.setdefault(mid, text or "")
    return found
