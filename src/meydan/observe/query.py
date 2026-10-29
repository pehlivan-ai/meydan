"""Read models: what a principal may see of the runtime, shaped for screens and reports.

Every function takes the principal first and applies the app's `Visibility` inside the query;
there is no unscoped variant, so a screen can't forget to filter. Which rows a person sees
(everyone's conversations or only their own) and which parts of them (message text, addresses,
agent details, costs) follow the visibility rules; audited items write each read to the audit
table. The default is `Visibility.need_to_know()`.
"""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import Connection, and_, case, func, insert, select, text
from sqlalchemy.dialects.postgresql import distinct_on

from meydan.auth.core import Principal
from meydan.runtime.tables import RuntimeTables
from meydan.visibility import Rule, Visibility, mask

NEED_TO_KNOW = Visibility.need_to_know()


class NotAllowedError(Exception):
    """The principal lacks a scope this read needs."""


def _need(who: Principal, rule: Rule) -> None:
    if not who.has(rule.scope):
        raise NotAllowedError(rule.scope)


def _shown(who: Principal, rule: Rule, value: str | None) -> str | None:
    """The value as this person may see it: in full, masked or hidden (None)."""
    if value is None or who.has(rule.scope):
        return value
    return mask(value) if rule.otherwise == "mask" else None


def _audit(
    conn: Connection,
    t: RuntimeTables,
    who: Principal,
    rule: Rule,
    action: str,
    subject_id: uuid.UUID | None = None,
) -> None:
    """Record that this person was shown an audited item."""
    if rule.audit and who.has(rule.scope):
        conn.execute(
            insert(t.audit).values(actor_id=who.actor_id, action=action, subject_id=subject_id)
        )


def _clock():
    return func.clock_timestamp()


# --- overview --------------------------------------------------------------------------------


class Overview(BaseModel):
    since: datetime
    turns: int
    replied: int
    failed: int
    gave_up: int
    cost_usd: Decimal | None
    cost_exact_share: float | None  # share of cost reported by providers (vs. estimated)
    avg_turn_s: float | None
    slowest_turn_s: float | None
    active_conversations: int
    messages_in: int
    pending_sends: int
    dead_letters: int


def overview(
    conn: Connection,
    t: RuntimeTables,
    who: Principal,
    hours: int = 24,
    *,
    visibility: Visibility = NEED_TO_KNOW,
) -> Overview:
    _need(who, visibility.operations)
    since = conn.scalar(select(_clock() - timedelta(hours=hours)))
    tu, i, o = t.turns, t.inbox, t.outbox
    seconds = func.extract("epoch", tu.c.finished_at - tu.c.started_at)
    exact = func.sum(case((tu.c.cost_source == "provider", tu.c.cost_usd), else_=0))
    row = conn.execute(
        select(
            func.count(),
            func.count().filter(tu.c.status == "replied"),
            func.count().filter(tu.c.status == "failed"),
            func.count().filter(tu.c.status == "gave_up"),
            func.sum(tu.c.cost_usd),
            exact,
            func.avg(seconds),
            func.max(seconds),
            func.count(tu.c.conversation_id.distinct()),
        ).where(tu.c.started_at >= since)
    ).one()
    messages_in = conn.scalar(select(func.count()).where(i.c.received_at >= since))
    pending = conn.scalar(select(func.count()).where(o.c.status == "pending"))
    dead = conn.scalar(select(func.count()).where(o.c.status == "dead", o.c.updated_at >= since))
    cost = row[4] if visibility.sees(who, "costs") else None
    return Overview(
        since=since,
        turns=row[0],
        replied=row[1],
        failed=row[2],
        gave_up=row[3],
        cost_usd=cost,
        cost_exact_share=float(row[5] / cost) if cost else None,
        avg_turn_s=round(float(row[6]), 2) if row[6] is not None else None,
        slowest_turn_s=round(float(row[7]), 2) if row[7] is not None else None,
        active_conversations=row[8],
        messages_in=messages_in or 0,
        pending_sends=pending or 0,
        dead_letters=dead or 0,
    )


# --- conversations ---------------------------------------------------------------------------


class ConversationRow(BaseModel):
    id: uuid.UUID
    channel: str
    actor_id: uuid.UUID | None
    name: str | None
    address: str | None  # in full, masked or None, per visibility
    last_at: datetime | None
    messages_in: int
    turns: int
    cost_usd: Decimal | None


def _peer(t: RuntimeTables):
    """conversation -> the peer's actor, name and one address (a phone number first)."""
    p, c, a, ident = t.participants, t.claims, t.actors, t.identities
    return (
        select(
            p.c.conversation_id,
            c.c.actor_id,
            a.c.name,
            ident.c.value.label("address"),
        )
        .join(ident, ident.c.id == p.c.identity_id)
        .outerjoin(
            c,
            and_(c.c.identity_id == p.c.identity_id, c.c.state == "active", c.c.valid_to.is_(None)),
        )
        .outerjoin(a, a.c.id == c.c.actor_id)
        .where(p.c.role == "peer")
        .order_by(p.c.conversation_id, (ident.c.scheme != "tel"), p.c.joined_at)
        .ext(distinct_on(p.c.conversation_id))
        .subquery()
    )


def _own_conversations(t: RuntimeTables, actor_id: uuid.UUID):
    p, c = t.participants, t.claims
    return (
        select(p.c.conversation_id)
        .join(c, and_(c.c.identity_id == p.c.identity_id, c.c.state == "active"))
        .where(c.c.actor_id == actor_id, c.c.valid_to.is_(None))
    )


def conversations(
    conn: Connection,
    t: RuntimeTables,
    who: Principal,
    limit: int = 50,
    *,
    visibility: Visibility = NEED_TO_KNOW,
) -> list[ConversationRow]:
    cv, i, tu = t.conversations, t.inbox, t.turns
    peer = _peer(t)
    of_inbox = i.c.conversation_id == cv.c.id
    of_turns = tu.c.conversation_id == cv.c.id
    q = (
        select(
            cv.c.id,
            cv.c.channel,
            peer.c.actor_id,
            peer.c.name,
            peer.c.address,
            select(func.max(i.c.received_at)).where(of_inbox).scalar_subquery().label("last_at"),
            select(func.count()).where(of_inbox).scalar_subquery().label("messages_in"),
            select(func.count()).where(of_turns).scalar_subquery().label("turns"),
            select(func.sum(tu.c.cost_usd)).where(of_turns).scalar_subquery().label("cost"),
        )
        .outerjoin(peer, peer.c.conversation_id == cv.c.id)
        .order_by(text("last_at DESC NULLS LAST"))
        .limit(min(limit, 200))
    )
    if not visibility.sees(who, "all_conversations"):
        q = q.where(cv.c.id.in_(_own_conversations(t, who.actor_id)))
    costs = visibility.sees(who, "costs")
    rows = [
        ConversationRow(
            id=r.id,
            channel=r.channel,
            actor_id=r.actor_id,
            name=r.name,
            address=_shown(who, visibility.addresses, r.address),
            last_at=r.last_at,
            messages_in=r.messages_in,
            turns=r.turns,
            cost_usd=r.cost if costs else None,
        )
        for r in conn.execute(q)
    ]
    if rows:
        _audit(conn, t, who, visibility.addresses, "read:addresses")
    return rows


# --- one conversation ------------------------------------------------------------------------


class Line(BaseModel):
    at: datetime
    direction: str  # "in" | "out"
    text: str | None  # None: redacted (no `messages:read`)
    turn_id: uuid.UUID | None


class TurnView(BaseModel):
    id: uuid.UUID
    status: str
    started_at: datetime
    seconds: float
    received_at: datetime | None  # first message of the turn
    read_at: datetime | None
    typing_at: datetime | None
    first_sent_at: datetime | None
    model: str | None
    model_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None
    cost_source: str | None
    error: str | None


class ConversationDetail(BaseModel):
    id: uuid.UUID
    channel: str
    lines: list[Line]
    turns: list[TurnView]  # empty when agent details are not visible
    text_redacted: bool
    details_hidden: bool


def conversation(
    conn: Connection,
    t: RuntimeTables,
    who: Principal,
    conversation_id: uuid.UUID,
    limit: int = 100,
    *,
    visibility: Visibility = NEED_TO_KNOW,
) -> ConversationDetail | None:
    cv, i, o, tu = t.conversations, t.inbox, t.outbox, t.turns
    mine = _own_conversations(t, who.actor_id).subquery()
    own = bool(conn.scalar(select(func.count()).where(mine.c.conversation_id == conversation_id)))
    if not own and not visibility.sees(who, "all_conversations"):
        return None  # not found, as far as they can tell
    head = conn.execute(select(cv.c.id, cv.c.channel).where(cv.c.id == conversation_id)).first()
    if head is None:
        return None
    show_text = own or visibility.sees(who, "message_text")
    if not own:
        _audit(conn, t, who, visibility.message_text, "read:messages", conversation_id)
    show_details = visibility.sees(who, "agent_details")
    show_costs = visibility.sees(who, "costs")

    ins = conn.execute(
        select(i.c.received_at, i.c.text, i.c.processed_at)
        .where(i.c.conversation_id == conversation_id)
        .order_by(i.c.received_at.desc())
        .limit(limit)
    ).all()
    outs = conn.execute(
        select(o.c.created_at, o.c.message, o.c.turn_id)
        .where(o.c.conversation_id == conversation_id)
        .order_by(o.c.created_at.desc())
        .limit(limit)
    ).all()
    lines = [Line(at=r.received_at, direction="in", text=r.text, turn_id=None) for r in ins]
    lines += [
        Line(
            at=r.created_at,
            direction="out",
            text=(r.message or {}).get("text"),
            turn_id=r.turn_id,
        )
        for r in outs
    ]
    lines.sort(key=lambda line: line.at)
    if not show_text:
        lines = [line.model_copy(update={"text": None}) for line in lines]

    # Per turn: the inbound messages it answered (processed when the turn finished) and its sends.
    first_sent = select(func.min(o.c.sent_at)).where(o.c.turn_id == tu.c.id).scalar_subquery()
    answered = and_(
        i.c.conversation_id == tu.c.conversation_id,
        i.c.processed_at.between(tu.c.started_at, tu.c.finished_at + timedelta(seconds=5)),
    )
    turns = (
        []
        if not show_details
        else conn.execute(
            select(
                tu,
                select(func.min(i.c.received_at))
                .where(answered)
                .scalar_subquery()
                .label("received"),
                select(func.min(i.c.read_at)).where(answered).scalar_subquery().label("read"),
                select(func.min(i.c.typing_at)).where(answered).scalar_subquery().label("typing"),
                first_sent.label("first_sent"),
            )
            .where(tu.c.conversation_id == conversation_id)
            .order_by(tu.c.started_at.desc())
            .limit(limit)
        ).all()
    )
    return ConversationDetail(
        id=head.id,
        channel=head.channel,
        lines=lines[-limit:],
        turns=[
            TurnView(
                id=r.id,
                status=r.status,
                started_at=r.started_at,
                seconds=round((r.finished_at - r.started_at).total_seconds(), 2),
                received_at=r.received,
                read_at=r.read,
                typing_at=r.typing,
                first_sent_at=r.first_sent,
                model=r.model,
                model_calls=r.model_calls,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                cost_usd=r.cost_usd if show_costs else None,
                cost_source=r.cost_source if show_costs else None,
                error=r.error,
            )
            for r in reversed(turns)
        ],
        text_redacted=not show_text,
        details_hidden=not show_details,
    )


# --- costs -----------------------------------------------------------------------------------


class CostDay(BaseModel):
    day: date
    turns: int
    cost_usd: Decimal | None


class CostModel(BaseModel):
    model: str | None
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None


class CostReport(BaseModel):
    days: list[CostDay]
    models: list[CostModel]
    total_usd: Decimal | None


def costs(
    conn: Connection,
    t: RuntimeTables,
    who: Principal,
    days: int = 30,
    *,
    visibility: Visibility = NEED_TO_KNOW,
) -> CostReport:
    _need(who, visibility.costs)
    tu, u = t.turns, t.usage
    since = conn.scalar(select(_clock() - timedelta(days=days)))
    day = func.date_trunc("day", tu.c.started_at).label("day")
    by_day = conn.execute(
        select(day, func.count(), func.sum(tu.c.cost_usd))
        .where(tu.c.started_at >= since)
        .group_by(day)
        .order_by(day)
    ).all()
    by_model = conn.execute(
        select(
            u.c.model,
            func.count(),
            func.sum(u.c.input_tokens),
            func.sum(u.c.output_tokens),
            func.sum(u.c.cost_usd),
        )
        .where(u.c.at >= since)
        .group_by(u.c.model)
        .order_by(func.sum(u.c.cost_usd).desc().nulls_last())
    ).all()
    total = sum((r[2] for r in by_day if r[2] is not None), Decimal(0)) if by_day else None
    return CostReport(
        days=[CostDay(day=r[0].date(), turns=r[1], cost_usd=r[2]) for r in by_day],
        models=[
            CostModel(
                model=r[0],
                calls=r[1],
                input_tokens=r[2] or 0,
                output_tokens=r[3] or 0,
                cost_usd=r[4],
            )
            for r in by_model
        ],
        total_usd=total,
    )


# --- problems --------------------------------------------------------------------------------


class Problem(BaseModel):
    at: datetime
    kind: str  # "turn_failed" | "turn_gave_up" | "send_dead"
    conversation_id: uuid.UUID
    error: str | None


def problems(
    conn: Connection,
    t: RuntimeTables,
    who: Principal,
    hours: int = 72,
    limit: int = 100,
    *,
    visibility: Visibility = NEED_TO_KNOW,
) -> list[Problem]:
    _need(who, visibility.operations)
    tu, o = t.turns, t.outbox
    since = conn.scalar(select(_clock() - timedelta(hours=hours)))
    failed = conn.execute(
        select(tu.c.started_at, tu.c.status, tu.c.conversation_id, tu.c.error).where(
            tu.c.status != "replied", tu.c.started_at >= since
        )
    ).all()
    dead = conn.execute(
        select(o.c.updated_at, o.c.conversation_id, o.c.last_error).where(
            o.c.status == "dead", o.c.updated_at >= since
        )
    ).all()
    items = [
        Problem(at=r[0], kind=f"turn_{r[1]}", conversation_id=r[2], error=r[3]) for r in failed
    ] + [Problem(at=r[0], kind="send_dead", conversation_id=r[1], error=r[2]) for r in dead]
    return sorted(items, key=lambda p: p.at, reverse=True)[:limit]


__all__ = [
    "ConversationDetail",
    "ConversationRow",
    "CostReport",
    "NotAllowedError",
    "Overview",
    "Problem",
    "conversation",
    "conversations",
    "costs",
    "mask",
    "overview",
    "problems",
]
