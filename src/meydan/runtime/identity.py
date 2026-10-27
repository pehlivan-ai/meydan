"""Who is who: identities, claims and settlement; and which conversation a message belongs to.

Model (docs/identity.md): an identity is an immutable address on a channel; an actor (person,
agent, org) owns identities through claims. Claims are append-only evidence: a link is changed by
closing one claim and opening another, never by editing rows, so every decision can be audited
and undone. Messages point to identities, so history never moves when ownership does.

Default settlement policy (what links automatically):
- `first_contact`: an unknown address writes: a new person actor owns it.
- `channel_cooccurrence`: the channel itself asserts, in one signed message, that two addresses
  are the same sender (WhatsApp: phone number and BSUID). Unowned addresses join the owner.
- Nothing else links on its own. If co-occurring addresses already belong to two different
  actors, a `proposed` claim is recorded for the app (or a person) to decide; nothing is merged.

All functions take the caller's connection and run inside its transaction.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from meydan.channels.base import Address
from meydan.runtime.tables import RuntimeTables

log = logging.getLogger("meydan.identity")


@dataclass(frozen=True)
class Routed:
    """Where an inbound message lands."""

    conversation_id: uuid.UUID
    sender_identity_id: uuid.UUID
    actor_id: uuid.UUID  # the sender's owner at the time of the message
    endpoint_identity_id: uuid.UUID


def lock(conn: Connection, *addresses: Address) -> None:
    """Serialize settlement per address (two webhooks for a new person must not make two actors).

    Locks are taken in a fixed order to avoid deadlocks; released at commit."""
    for key in sorted({str(a) for a in addresses}):
        conn.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(f"identity:{key}", 0)))
        )


def resolve(conn: Connection, t: RuntimeTables, address: Address, **attrs: Any) -> uuid.UUID:
    """The identity for an address, created if new. `attrs` (names, usernames) are merged in."""
    i = t.identities
    stmt = pg_insert(i).values(
        scheme=address.scheme, scope=address.scope, value=address.value, attrs=attrs
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[i.c.scheme, i.c.scope, i.c.value],
        set_={"attrs": i.c.attrs.op("||")(stmt.excluded.attrs), "last_seen_at": func.now()},
    ).returning(i.c.id)
    return conn.execute(stmt).scalar_one()


def owner(conn: Connection, t: RuntimeTables, identity_id: uuid.UUID) -> uuid.UUID | None:
    """The actor that currently owns an identity (its active, open claim), if any."""
    c = t.claims
    return conn.scalar(
        select(c.c.actor_id).where(
            c.c.identity_id == identity_id, c.c.state == "active", c.c.valid_to.is_(None)
        )
    )


def new_actor(
    conn: Connection,
    t: RuntimeTables,
    kind: str,
    name: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> uuid.UUID:
    values: dict[str, Any] = {"kind": kind, "name": name}
    if actor_id:
        values["id"] = actor_id  # apps may reuse an existing id (e.g. a user row's)
    return conn.execute(insert(t.actors).values(values).returning(t.actors.c.id)).scalar_one()


def claim(
    conn: Connection,
    t: RuntimeTables,
    identity_id: uuid.UUID,
    actor_id: uuid.UUID,
    basis: str,
    *,
    state: str = "active",
    evidence: dict[str, Any] | None = None,
    by_actor_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
) -> None:
    """Record that an identity belongs (or, `proposed`, may belong) to an actor."""
    conn.execute(
        insert(t.claims).values(
            identity_id=identity_id,
            actor_id=actor_id,
            state=state,
            basis=basis,
            evidence=evidence or {},
            by_actor_id=by_actor_id,
            batch_id=batch_id,
        )
    )


def settle(
    conn: Connection,
    t: RuntimeTables,
    sender: uuid.UUID,
    also: list[uuid.UUID],
    *,
    evidence: dict[str, Any],
    name: str | None = None,
    kind: str = "person",
) -> uuid.UUID:
    """Apply the default policy to a sender and the addresses the channel says are the same.

    Returns the actor that owns the sender afterwards."""
    owners = {i: owner(conn, t, i) for i in [sender, *also]}
    actor = owners[sender] or next((a for a in owners.values() if a), None)
    created = actor is None
    if actor is None:
        actor = new_actor(conn, t, kind, name)
    for identity, current in owners.items():
        if current == actor:
            continue
        basis = "first_contact" if identity == sender and created else "channel_cooccurrence"
        if current is None:
            claim(conn, t, identity, actor, basis, evidence=evidence)
        else:  # two actors already: record it, don't merge (the app or the person decides)
            _propose_once(conn, t, identity, actor, evidence)
            log.warning("identity conflict: co-occurring addresses belong to two actors")
    return actor


def _propose_once(
    conn: Connection,
    t: RuntimeTables,
    identity: uuid.UUID,
    actor: uuid.UUID,
    evidence: dict[str, Any],
) -> None:
    c = t.claims
    exists = conn.scalar(
        select(c.c.id).where(
            c.c.identity_id == identity,
            c.c.actor_id == actor,
            c.c.state == "proposed",
            c.c.valid_to.is_(None),
        )
    )
    if not exists:
        claim(conn, t, identity, actor, "channel_cooccurrence", state="proposed", evidence=evidence)


def endpoint(conn: Connection, t: RuntimeTables, address: Address) -> uuid.UUID:
    """Our side of a conversation (a number, mailbox, agent). Owned by an agent actor, created on
    first sight; apps can name it or claim the endpoint for their own agent actor."""
    identity = resolve(conn, t, address)
    if owner(conn, t, identity) is None:
        claim(conn, t, identity, new_actor(conn, t, "agent"), "endpoint")
    return identity


def direct_conversation(
    conn: Connection,
    t: RuntimeTables,
    channel: str,
    endpoint_id: uuid.UUID,
    actor_id: uuid.UUID,
    identities: list[uuid.UUID],
) -> uuid.UUID:
    """The direct conversation between our endpoint and an actor, created if there is none.

    Found through *any* identity the actor owns, so a person whose address changes (phone hidden,
    BSUID rotated) stays in the same conversation; new identities are added as participants."""
    cv, p, c = t.conversations, t.participants, t.claims
    found = conn.scalar(
        select(cv.c.id)
        .join(p, p.c.conversation_id == cv.c.id)
        .join(
            c,
            (c.c.identity_id == p.c.identity_id) & (c.c.state == "active") & c.c.valid_to.is_(None),
        )
        .where(
            cv.c.kind == "direct",
            cv.c.channel == channel,
            cv.c.endpoint_identity_id == endpoint_id,
            c.c.actor_id == actor_id,
            p.c.role == "peer",
        )
        .order_by(cv.c.id)
        .limit(1)
    )
    if found is None:
        found = conn.execute(
            insert(cv)
            .values(kind="direct", channel=channel, endpoint_identity_id=endpoint_id)
            .returning(cv.c.id)
        ).scalar_one()
        conn.execute(insert(p).values(conversation_id=found, identity_id=endpoint_id, role="self"))
    for identity in identities:
        conn.execute(
            pg_insert(p)
            .values(conversation_id=found, identity_id=identity, role="peer")
            .on_conflict_do_nothing()
        )
    return found


def route(
    conn: Connection,
    t: RuntimeTables,
    *,
    channel: str,
    to: Address,
    sender: Address,
    also: tuple[Address, ...] = (),
    name: str | None = None,
    attrs: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> Routed:
    """Resolve, settle and find the conversation for one inbound message (or a planned contact)."""
    lock(conn, sender, *also)
    endpoint_id = endpoint(conn, t, to)
    sender_attrs = {k: v for k, v in {"name": name, **(attrs or {})}.items() if v}
    sender_id = resolve(conn, t, sender, **sender_attrs)
    also_ids = [resolve(conn, t, a, **sender_attrs) for a in also if a != sender]
    actor = settle(conn, t, sender_id, also_ids, evidence=evidence or {}, name=name)
    if name:
        conn.execute(
            update(t.actors)
            .where(t.actors.c.id == actor, t.actors.c.name.is_(None))
            .values(name=name, updated_at=func.now())
        )
    conversation = direct_conversation(conn, t, channel, endpoint_id, actor, [sender_id, *also_ids])
    return Routed(conversation, sender_id, actor, endpoint_id)
