"""Reaching a person outside a turn: where they last wrote, and whether we may write freely there.

A message we start (a payment card, a reminder, a confirmation) goes where the person last wrote
to us: that conversation, from the endpoint they wrote to, to the address they wrote from. Inside
the channel's reply window (meydan/rules.py: WhatsApp 24 hours) it may be free-form; outside it,
only what the channel allows instead (an approved template). `busy` says a turn is about to run or
running in a conversation (a message of theirs not yet answered): a message we start then waits,
so it follows the agent's reply instead of cutting into it (meydan.pay.notices).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, func, select

from meydan.runtime.tables import RuntimeTables


@dataclass(frozen=True)
class Reach:
    channel: str
    conversation_id: uuid.UUID
    identity_id: uuid.UUID | None  # the address they wrote from
    to: str  # our endpoint they wrote to: we write from it
    sender: str  # their address on the channel: we write to it
    last_at: datetime  # their last message
    window_open: bool  # free-form allowed now


def reach(
    conn: Connection,
    t: RuntimeTables,
    rules: Any,
    actor_id: uuid.UUID,
    *,
    channel: str | None = None,
) -> Reach | None:
    """Where this person last wrote to us (on `channel`, or any), through an address they hold
    (an active claim). None if they never wrote. `rules`: the runtime's (`Runtime.rules`)."""
    i, c = t.inbox, t.claims
    q = (
        select(
            i.c.channel, i.c.conversation_id, i.c.sender_identity_id, i.c.to, i.c.sender,
            i.c.received_at, func.now().label("now"),
        )
        .join(c, c.c.identity_id == i.c.sender_identity_id)
        .where(c.c.actor_id == actor_id, c.c.state == "active", c.c.valid_to.is_(None))
        .order_by(i.c.received_at.desc())
        .limit(1)
    )  # fmt: skip
    if channel is not None:
        q = q.where(i.c.channel == channel)
    last = conn.execute(q).first()
    if last is None:
        return None
    window = rules.of(last.channel).reply_window
    return Reach(
        channel=last.channel, conversation_id=last.conversation_id,
        identity_id=last.sender_identity_id, to=last.to, sender=last.sender,
        last_at=last.received_at,
        window_open=window is None or last.now - last.received_at <= window,
    )  # fmt: skip


def busy(conn: Connection, t: RuntimeTables, conversation_id: uuid.UUID) -> bool:
    """Whether a turn will run or is running in this conversation: a message of theirs there is
    not answered yet (the worker marks it processed when its turn's reply is queued)."""
    i = t.inbox
    q = select(i.c.id).where(i.c.conversation_id == conversation_id, i.c.processed_at.is_(None))
    return conn.execute(q.limit(1)).first() is not None


__all__ = ["Reach", "busy", "reach"]
