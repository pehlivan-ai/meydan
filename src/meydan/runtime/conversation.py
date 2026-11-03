"""What was said in a conversation, both directions, on one clock."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, select

from meydan.runtime.tables import RuntimeTables


@dataclass(frozen=True)
class Said:
    """One line of a conversation's transcript: something received or sent."""

    at: datetime  # database clock: when we received it / queued it (one clock for both sides)
    direction: str  # "in" | "out"
    identity_id: uuid.UUID | None  # who said it (in) / to whom (out)
    text: str
    turn_id: uuid.UUID | None = None  # outbound: messages of one reply share it


def transcript(
    conn: Connection, t: RuntimeTables, conversation_id: uuid.UUID, limit: int = 30
) -> list[Said]:
    """The last `limit` things said in a conversation, oldest first.

    Inbound: messages already consumed by a turn. Outbound: everything queued except dead
    letters. Both sides are ordered on the database clock: a sender's device clock (the
    channel timestamp) must not interleave with ours."""
    i, o = t.inbox, t.outbox
    ins = conn.execute(
        select(i.c.received_at, i.c.id, i.c.sender_identity_id, i.c.text)
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
    lines = [(at, id_, Said(at, "in", who, text or "")) for at, id_, who, text in ins]
    lines += [
        (at, id_, Said(at, "out", who, (msg or {}).get("text") or "", turn))
        for at, id_, who, msg, turn in outs
    ]
    return [said for _, _, said in sorted(lines, key=lambda x: (x[0], x[1]))[-limit:]]
