"""The desk: people on the team working the conversations next to the agent.

Read state, taking a conversation over and handing it back, and writing to someone as yourself.
The core has no web framework (like `meydan.auth`); `meydan.desk.web` (extra `api`) serves it.

- **Reading.** Each reader has a mark per conversation (`seen_until`); what came in after it is
  unread. A reader is a principal as text: a person's actor id, or, for a service key, whom the
  service reads for (`Principal.on_behalf_of`) — so a dashboard's users each keep their own marks.
- **Holding.** A conversation a person takes over is held: the agent does not answer it (the
  runtime's worker checks, `is_held`), while every message is still received and recorded. A hold
  has an end (`until`) or lasts until handed back (`release`).
- **Writing.** A person on the team writes as themselves: the message is queued with
  `author="staff"` and their actor, so every reader sees who wrote it. On channels with a
  customer-service window (WhatsApp: 24 hours after their last message) a free-form message is
  refused once the window has closed (`WindowClosedError`); a template is the way back.

Staff are actors too. A person known to another application (a dashboard's user) gets an actor
here through an identity in that application (`staff_actor`): scheme "staff", their id there.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Connection, func, select, update
from sqlalchemy.dialects.postgresql import insert

from meydan.channels.base import Address, Outbound
from meydan.runtime import identity
from meydan.runtime.tables import RuntimeTables

# How long after their last message a channel lets us write freely. Not listed: no window.
WINDOWS = {"whatsapp": timedelta(hours=24)}


class WindowClosedError(Exception):
    """The channel's window for free-form messages has closed: send a template instead."""


class NotFoundError(Exception):
    """No such conversation, or no message from them to answer."""


@dataclass(frozen=True)
class Hold:
    held_by: str
    until: datetime | None  # None: until handed back
    at: datetime


# --- reading -------------------------------------------------------------------------------


def mark_read(
    conn: Connection, t: RuntimeTables, conversation_id: uuid.UUID, reader: str
) -> datetime:
    """This reader has seen everything that came in so far. Returns the mark (never moves back)."""
    r = t.desk_reads
    now = conn.scalar(select(func.now()))
    stmt = insert(r).values(conversation_id=conversation_id, reader=reader[:200], seen_until=now)
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[r.c.conversation_id, r.c.reader],
            set_={"seen_until": func.greatest(r.c.seen_until, stmt.excluded.seen_until)},
        )
    )
    return now


def unread(
    conn: Connection, t: RuntimeTables, reader: str, conversation_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """How many of their messages each conversation has that this reader hasn't seen."""
    if not conversation_ids:
        return {}
    i, r = t.inbox, t.desk_reads
    mark = (
        select(r.c.seen_until)
        .where(r.c.conversation_id == i.c.conversation_id, r.c.reader == reader[:200])
        .scalar_subquery()
    )
    rows = conn.execute(
        select(i.c.conversation_id, func.count())
        .where(
            i.c.conversation_id.in_(conversation_ids),
            (mark.is_(None)) | (i.c.received_at > mark),
        )
        .group_by(i.c.conversation_id)
    )
    return {row[0]: row[1] for row in rows}


# --- holding -------------------------------------------------------------------------------


def hold(
    conn: Connection,
    t: RuntimeTables,
    conversation_id: uuid.UUID,
    held_by: str,
    for_: timedelta | None = None,
) -> Hold:
    """Take a conversation over: the agent stops answering it, for `for_` or until `release`."""
    h = t.desk_holds
    now = conn.scalar(select(func.now()))
    until = now + for_ if for_ is not None else None
    values = {
        "conversation_id": conversation_id,
        "held_by": held_by[:200],
        "until": until,
        "at": now,
    }
    stmt = insert(h).values(values)
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[h.c.conversation_id],
            set_={"held_by": stmt.excluded.held_by, "until": stmt.excluded.until, "at": now},
        )
    )
    return Hold(held_by[:200], until, now)


def release(conn: Connection, t: RuntimeTables, conversation_id: uuid.UUID) -> bool:
    """Hand it back to the agent. False: it wasn't held."""
    h = t.desk_holds
    return bool(conn.execute(h.delete().where(h.c.conversation_id == conversation_id)).rowcount)


def holding(conn: Connection, t: RuntimeTables, conversation_id: uuid.UUID) -> Hold | None:
    """The hold on a conversation, if one is in force now."""
    found = holds(conn, t, [conversation_id])
    return found.get(conversation_id)


def holds(
    conn: Connection, t: RuntimeTables, conversation_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Hold]:
    if not conversation_ids:
        return {}
    h = t.desk_holds
    rows = conn.execute(
        select(h).where(
            h.c.conversation_id.in_(conversation_ids),
            (h.c.until.is_(None)) | (h.c.until > func.now()),
        )
    )
    return {r.conversation_id: Hold(r.held_by, r.until, r.at) for r in rows}


def is_held(conn: Connection, t: RuntimeTables, conversation_id: uuid.UUID) -> bool:
    """For the worker: a held conversation gets no answer from the agent."""
    return holding(conn, t, conversation_id) is not None


# --- staff and writing ---------------------------------------------------------------------

STAFF = "staff"  # the identity scheme of a person known to another application


def staff_actor(conn: Connection, t: RuntimeTables, ref: str, name: str | None = None) -> uuid.UUID:
    """The actor of a person on the team known to another application by `ref` (e.g. a dashboard
    user's id); made the first time, its display name kept up to date when one is given."""
    ident = identity.resolve(conn, t, Address(STAFF, ref[:255]))
    actor = identity.owner(conn, t, ident)
    if actor is None:
        actor = identity.new_actor(conn, t, "person", name)
        identity.claim(conn, t, ident, actor, "imported", evidence={"ref": ref[:255]})
    elif name:
        conn.execute(update(t.actors).where(t.actors.c.id == actor).values(name=name[:255]))
    return actor


def send(
    conn: Connection,
    runtime,
    conversation_id: uuid.UUID,
    text: str,
    by: uuid.UUID,
    *,
    hold_for: timedelta | None = timedelta(minutes=30),
    held_by: str | None = None,
) -> uuid.UUID:
    """A person on the team writes `text` in a conversation, as themselves (`by`, an actor).

    It goes to whoever wrote last, from the endpoint they wrote to, queued like any message (paced,
    retried, its delivery recorded). Writing takes the conversation over for `hold_for` (None: no
    hold), so the agent doesn't answer over them. Raises NotFoundError (nobody to answer) or
    WindowClosedError (the channel's free-form window has closed). Returns the turn id."""
    t = runtime.t
    i = t.inbox
    last = conn.execute(
        select(i.c.channel, i.c.to, i.c.sender, i.c.sender_identity_id, i.c.received_at)
        .where(i.c.conversation_id == conversation_id)
        .order_by(i.c.received_at.desc())
        .limit(1)
    ).first()
    if last is None:
        raise NotFoundError("no message from them in this conversation")
    window = WINDOWS.get(last.channel)
    if window is not None and conn.scalar(select(func.now())) - last.received_at > window:
        raise WindowClosedError(f"{last.channel}: more than {window} since their last message")
    turn = runtime.enqueue(
        conn,
        channel=last.channel,
        conversation_id=conversation_id,
        from_number=last.to,
        to=last.sender,
        recipient_identity_id=last.sender_identity_id,
        messages=[Outbound.text_message(text)],
        author="staff",
        author_actor_id=by,
    )
    if hold_for is not None:
        hold(conn, t, conversation_id, held_by or str(by), hold_for)
    return turn
