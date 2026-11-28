"""The operator: a person on the team stepping into a conversation the agent is having.

The vocabulary, by who starts it:

- **takeover** (a person starts it): they take the conversation over; while it holds, the agent
  does not answer it (the runtime's worker checks, `is_taken_over`), and every message is still
  received and recorded. A takeover has an end (`until`) or lasts until handed back.
- **handback** (a person starts it): they give it back to the agent.
- **handoff** (the machine starts it): the agent hands a conversation to a person. Not built yet.

And next to it:

- **Reading.** Each reader has a mark per conversation (`seen_until`); what came in after it is
  unread. A reader is a principal as text: a person's actor id, or, for a service key, whom the
  service acts for (`Principal.on_behalf_of`), so a dashboard's users each keep their own marks.
- **Writing.** A person writes as themselves: the message is queued with `author="staff"` and
  their actor, so every reader sees who wrote it. On channels with a customer-service window
  (WhatsApp: 24 hours after their last message) free text is refused once it has closed
  (`WindowClosedError`); a template is the way back.

Staff are actors too. A person known to another application (a dashboard's user) gets an actor
here through an identity in that application (`staff_actor`): scheme "staff", their id there.
The core has no web framework (like `meydan.auth`); `meydan.operator.web` (extra `api`) serves it.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Connection, func, select, update
from sqlalchemy.dialects.postgresql import insert

from meydan.channels.base import Address, Outbound
from meydan.runtime import identity
from meydan.runtime.tables import RuntimeTables


class WindowClosedError(Exception):
    """The channel's window for free-form messages has closed: send a template instead, where the
    channel has templates (WhatsApp); on Instagram, wait for the person to write."""


class NotFoundError(Exception):
    """No such conversation, or no message from them to answer."""


STAFF_READ = "conversation.read"  # a person on the team read it (meydan.bus)


@dataclass(frozen=True)
class Takeover:
    taken_by: str
    until: datetime | None  # None: until handed back
    at: datetime


# --- reading -------------------------------------------------------------------------------


def mark_read(
    conn: Connection, t: RuntimeTables, conversation_id: uuid.UUID, reader: str
) -> datetime:
    """This reader has seen everything that came in so far. Returns the mark (never moves back).

    Also a fact, `conversation.read` (meydan.bus): a person on the team has the conversation in
    front of them, so the runtime marks what they now saw "seen" on the channel. While a
    conversation is taken over that is the only "seen" its writer gets (meydan.runtime.ingest)."""
    from meydan.bus import emit  # noqa: PLC0415

    r = t.operator_reads
    now = conn.scalar(select(func.now()))
    stmt = insert(r).values(conversation_id=conversation_id, reader=reader[:200], seen_until=now)
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[r.c.conversation_id, r.c.reader],
            set_={"seen_until": func.greatest(r.c.seen_until, stmt.excluded.seen_until)},
        )
    )
    cv = t.conversations
    channel = conn.scalar(select(cv.c.channel).where(cv.c.id == conversation_id))
    emit(conn, t, STAFF_READ, subject_id=conversation_id, channel=channel, data={"reader": reader})
    return now


def unread(
    conn: Connection, t: RuntimeTables, reader: str, conversation_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """How many of their messages each conversation has that this reader hasn't seen."""
    if not conversation_ids:
        return {}
    i, r = t.inbox, t.operator_reads
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


class AlreadyTakenError(Exception):
    """Someone else has this conversation; taking it over needs `replace`."""

    def __init__(self, hold: "Takeover"):
        super().__init__(f"taken over by {hold.taken_by}")
        self.hold = hold


def takeover(
    conn: Connection,
    t: RuntimeTables,
    conversation_id: uuid.UUID,
    taken_by: str,
    for_: timedelta | None = None,
    *,
    replace: bool = False,
    extend_only: bool = False,
) -> Takeover:
    """Take a conversation over: the agent stops answering it, for `for_` or until `handback`.

    Someone else's takeover is not overwritten unless `replace` (AlreadyTakenError). With
    `extend_only` (what writing does) an existing takeover is never shortened: one until handed
    back stays so. The agent's replies queued and not yet sent are cancelled: once a person has
    it, nothing the agent drafted before goes out."""
    h, o = t.operator_holds, t.outbox
    conn.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"operator:{conversation_id}", 0)))
    )
    now = conn.scalar(select(func.now()))
    until = now + for_ if for_ is not None else None
    held = takeover_of(conn, t, conversation_id)
    if held is not None and held.taken_by != taken_by[:200] and not replace:
        raise AlreadyTakenError(held)
    if (
        extend_only
        and held is not None
        and (held.until is None or (until is not None and held.until >= until))
    ):
        until = held.until  # writing never shortens a takeover
    values = {
        "conversation_id": conversation_id,
        "taken_by": taken_by[:200],
        "until": until,
        "at": now,
    }
    stmt = insert(h).values(values)
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[h.c.conversation_id],
            set_={"taken_by": stmt.excluded.taken_by, "until": stmt.excluded.until, "at": now},
        )
    )
    conn.execute(
        update(o)
        .where(
            o.c.conversation_id == conversation_id, o.c.status == "pending", o.c.author == "agent"
        )
        .values(status="cancelled", last_error="taken over by a person", updated_at=now)
    )
    return Takeover(taken_by[:200], until, now)


def handback(conn: Connection, t: RuntimeTables, conversation_id: uuid.UUID) -> bool:
    """Hand it back to the agent. False: it wasn't taken over."""
    h = t.operator_holds
    return bool(conn.execute(h.delete().where(h.c.conversation_id == conversation_id)).rowcount)


def takeover_of(conn: Connection, t: RuntimeTables, conversation_id: uuid.UUID) -> Takeover | None:
    """The hold on a conversation, if one is in force now."""
    found = takeovers(conn, t, [conversation_id])
    return found.get(conversation_id)


def takeovers(
    conn: Connection, t: RuntimeTables, conversation_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Takeover]:
    if not conversation_ids:
        return {}
    h = t.operator_holds
    rows = conn.execute(
        select(h).where(
            h.c.conversation_id.in_(conversation_ids),
            (h.c.until.is_(None)) | (h.c.until > func.now()),
        )
    )
    return {r.conversation_id: Takeover(r.taken_by, r.until, r.at) for r in rows}


def is_taken_over(conn: Connection, t: RuntimeTables, conversation_id: uuid.UUID) -> bool:
    """For the worker: a conversation taken over gets no answer from the agent."""
    return takeover_of(conn, t, conversation_id) is not None


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
    take_over_for: timedelta | None = timedelta(minutes=30),
    taken_by: str | None = None,
) -> uuid.UUID:
    """A person on the team writes `text` in a conversation, as themselves (`by`, an actor).

    It goes to whoever wrote last, from the endpoint they wrote to, queued like any message (paced,
    retried, its delivery recorded). Writing takes it over for `take_over_for` (None: not),
    so the agent doesn't answer over them. Raises NotFoundError (nobody to answer) or
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
    window = runtime.rules.of(last.channel).reply_window  # the channel's rules, meydan/rules.py
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
    # What they wrote before this is answered, by a person: the agent won't answer it later.
    i2 = t.inbox
    conn.execute(
        update(i2)
        .where(i2.c.conversation_id == conversation_id, i2.c.processed_at.is_(None))
        .values(processed_at=func.now())
    )
    if take_over_for is not None:
        takeover(
            conn,
            t,
            conversation_id,
            taken_by or str(by),
            take_over_for,
            replace=True,
            extend_only=True,
        )
    return turn
