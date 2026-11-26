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
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import Connection, and_, case, func, insert, or_, select, text
from sqlalchemy.dialects.postgresql import distinct_on

from meydan.auth.core import Principal
from meydan.channels.base import seen_kind
from meydan.observe.engagement import SEEN_BIAS, engagement
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
            insert(t.audit).values(
                actor_id=who.actor_id,
                action=action,
                subject_id=subject_id,
                on_behalf_of=who.on_behalf_of,
            )
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
    last_at: datetime | None  # their last message
    messages_in: int
    turns: int
    cost_usd: Decimal | None
    # The last message either way: when, which way, who wrote it, and its text (None: redacted,
    # as in the thread: someone else's text needs `messages:read`).
    last_message_at: datetime | None = None
    last_direction: str | None = None  # "in" | "out"
    last_author: str | None = None  # person | agent | staff | system
    last_text: str | None = None


def _peer(t: RuntimeTables):
    """conversation -> the peer's actor, name and one address (a phone number first)."""
    p, c, a, ident = t.participants, t.claims, t.actors, t.identities
    return (
        select(
            p.c.conversation_id,
            c.c.actor_id,
            # An address nobody owns yet (a contact we started) shows the name we gave it.
            func.coalesce(a.c.name, ident.c.attrs["name"].astext).label("name"),
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
    only: uuid.UUID | None = None,
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
    if only is not None:
        q = q.where(cv.c.id == only)
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
        rows = _with_last_messages(conn, t, who, rows, visibility)
    return rows


def _template_words(conn: Connection, t: RuntimeTables, channel: str, outs) -> dict:
    """(name, language) -> (category, body, buttons) for the templates these messages used."""
    used = {(m.get("template"), m.get("language")) for m in (r.message or {} for r in outs)}
    used.discard((None, None))
    if not used:
        return {}
    tp = t.templates
    rows = conn.execute(
        select(tp.c.name, tp.c.language, tp.c.category, tp.c.body, tp.c.buttons).where(
            tp.c.channel == channel, tp.c.name.in_({n for n, _ in used})
        )
    )
    return {(r.name, r.language): (r.category, r.body, r.buttons or []) for r in rows}


def _said(message: dict, words: dict) -> dict:
    """What a sent message said: its text, or a template's body filled with its parameters."""
    name = message.get("template")
    if not name:
        return {"text": message.get("text")}
    category, body, buttons = words.get((name, message.get("language")), (None, None, []))
    if category == "AUTHENTICATION" or message.get("otp") is not None:
        return {"text": None, "template": name, "buttons": [], "code": True}  # never its words
    text = body
    if text is not None:
        for key, value in (message.get("params") or {}).items():
            text = text.replace("{{" + str(key) + "}}", str(value))
    return {"text": text, "template": name, "buttons": list(buttons)}


def _parts(message: dict, channel: str) -> dict:
    """The parts of a message we sent, as the person saw them."""
    kind = message.get("kind") or "text"
    out: dict = {"kind": kind}
    if kind == "image":
        out["image"] = ImagePart(
            media=message.get("media"), caption=message.get("caption"), alt=_alt(message)
        )
        out["text"] = message.get("caption")
    elif kind == "location" and message.get("lat") is not None:
        out["location"] = LocationPart(
            lat=message["lat"], lng=message["lng"], name=message.get("place"),
            address=message.get("address"),
        )  # fmt: skip
        out["text"] = None
    elif kind == "choices":
        options = [
            ChoiceOption(title=c["title"], description=c.get("description"))
            for c in message.get("choices") or []
        ]
        out["choices"] = ChoicesPart(
            question=message.get("body") or "", options=options,
            style=_choice_style(message, channel), label=message.get("choices_label"),
        )  # fmt: skip
        out["text"] = message.get("body")
    elif kind == "contact":
        out["contact"] = ContactPart(
            name=message.get("contact_name") or "", phone=message.get("contact_phone"),
            title=message.get("contact_title"), org=message.get("contact_org"),
        )  # fmt: skip
        out["text"] = None
    return out


def _alt(message: dict) -> str | None:
    text = message.get("text") or ""
    if text.startswith("[image: ") and "]" in text:
        return text[len("[image: ") : text.index("]")]
    return None


def _choice_style(message: dict, channel: str) -> str | None:
    if channel != "whatsapp":
        return None
    from meydan.channels.base import Outbound
    from meydan.channels.whatsapp import choice_style

    try:
        return choice_style(Outbound.from_payload(message))
    except (ValueError, TypeError):
        return None


def _theirs(kind: str, payload: dict | None, media: str | None = None) -> dict:
    """The parts of a message someone sent us, from the channel's payload."""
    out: dict = {"kind": kind, "tapped": kind in ("interactive", "button")}
    loc = (payload or {}).get("location") if kind == "location" else None
    if loc and loc.get("latitude") is not None:
        out["location"] = LocationPart(
            lat=float(loc["latitude"]), lng=float(loc["longitude"]), name=loc.get("name"),
            address=loc.get("address"),
        )  # fmt: skip
    if seen_kind(kind, payload) == "image":  # a photo, also one sent as a file
        part = (payload or {}).get(kind) or {}
        out["image"] = ImagePart(media=media, caption=part.get("caption"))
    return out


def _delivery(r) -> str:
    if r.read_at is not None:
        return "read"
    if r.delivered_at is not None:
        return "delivered"
    return r.status


def _with_last_messages(
    conn: Connection,
    t: RuntimeTables,
    who: Principal,
    rows: list[ConversationRow],
    visibility: Visibility,
) -> list[ConversationRow]:
    """Each row's last message either way, its author, and its text as the thread would show it."""
    i, o = t.inbox, t.outbox
    ids = [r.id for r in rows]
    last_in = {
        r.conversation_id: r
        for r in conn.execute(
            select(i.c.conversation_id, i.c.received_at.label("at"), i.c.text)
            .where(i.c.conversation_id.in_(ids))
            .ext(distinct_on(i.c.conversation_id))
            .order_by(i.c.conversation_id, i.c.received_at.desc())
        )
    }
    last_out = {
        r.conversation_id: r
        for r in conn.execute(
            select(o.c.conversation_id, o.c.created_at.label("at"), o.c.message, o.c.author)
            .where(o.c.conversation_id.in_(ids))
            .ext(distinct_on(o.c.conversation_id))
            .order_by(o.c.conversation_id, o.c.created_at.desc(), o.c.id.desc())
        )
    }
    own = {row.conversation_id for row in conn.execute(_own_conversations(t, who.actor_id))}
    reads_others = visibility.sees(who, "message_text")
    out, shown_others = [], False
    for row in rows:
        a, b = last_in.get(row.id), last_out.get(row.id)
        if a is None and b is None:
            out.append(row)
            continue
        inbound = b is None or (a is not None and a.at >= b.at)
        text_ = a.text if inbound else (b.message or {}).get("text")
        visible = row.id in own or reads_others
        shown_others |= visible and row.id not in own and text_ is not None
        out.append(row.model_copy(update={
            "last_message_at": a.at if inbound else b.at,
            "last_direction": "in" if inbound else "out",
            "last_author": "person" if inbound else b.author,
            "last_text": text_ if visible else None,
        }))  # fmt: skip
    if shown_others:
        _audit(conn, t, who, visibility.message_text, "read:messages")
    return out


# --- a file in a conversation ---------------------------------------------------------------------


def media(
    conn: Connection,
    t: RuntimeTables,
    who: Principal,
    key: str,
    *,
    visibility: Visibility = NEED_TO_KNOW,
    store=None,
) -> tuple[bytes, str] | None:
    """A file in a conversation (an image we sent, or a photo someone sent us), for a screen
    that shows it: its bytes and type. Shown like the messages it is in: to someone in one of
    those conversations, or to someone who may read others' messages (audited). None when there
    is no such file or the person may not see it."""
    from meydan import media as files

    o, i = t.outbox, t.inbox
    sent = select(o.c.conversation_id).where(o.c.message["media"].astext == key)
    got = select(i.c.conversation_id).where(i.c.media == key)
    convs = {r[0] for r in conn.execute(sent.union(got))}
    if not convs:
        return None
    own = {r.conversation_id for r in conn.execute(_own_conversations(t, who.actor_id))}
    if not convs & own:
        if not visibility.sees(who, "message_text"):
            return None
        _audit(conn, t, who, visibility.message_text, "read:media")
    try:
        return files.load(conn, t, key, store=store)
    except files.MediaMissingError:
        return None


# --- one conversation ------------------------------------------------------------------------


class ImagePart(BaseModel):
    media: str | None  # the file's key in meydan.media (GET /observe/media/{key}); None: not ours
    alt: str | None = None
    caption: str | None = None


class LocationPart(BaseModel):
    lat: float
    lng: float
    name: str | None = None
    address: str | None = None


class ChoiceOption(BaseModel):
    title: str
    description: str | None = None


class ChoicesPart(BaseModel):
    question: str
    options: list[ChoiceOption]
    style: str | None = None  # "buttons" | "list" as the channel shows them; None: unknown
    label: str | None = None  # the button that opens a list


class ContactPart(BaseModel):
    name: str
    phone: str | None = None
    title: str | None = None
    org: str | None = None


class Line(BaseModel):
    id: str  # "in:<id>" | "out:<id>": stable, for an app to point at one message
    at: datetime
    direction: str  # "in" | "out"
    text: str | None  # None: redacted (no `messages:read`)
    turn_id: uuid.UUID | None
    # Who wrote it: person (them) | agent | staff (`author_id`: who) | system. Recorded when the
    # message was queued (outbox.author), never inferred.
    author: str
    author_id: uuid.UUID | None = None
    # Ours only: pending | held | sent | delivered | read | failed | dead (receipts win over the
    # send status: a read message was delivered).
    status: str | None = None
    # A template we sent: its name, its body filled with this message's parameters (`text`), and
    # its buttons. An authentication template (a one-time code) shows no words at all.
    template: str | None = None
    buttons: list[str] = []
    # A one-time code (an authentication template): never shown, only that it was sent.
    code: bool = False
    # What kind of message it was, and its parts as the person saw them (with `text`, hidden
    # together). Ours: text | template | image | location | choices | contact. Theirs: the
    # channel's kind (text, interactive, button, image, location, ...).
    kind: str = "text"
    image: ImagePart | None = None
    location: LocationPart | None = None
    choices: ChoicesPart | None = None
    contact: ContactPart | None = None
    # Theirs: the message of ours it answers ("out:<id>"), and whether they tapped an option.
    reply_to: str | None = None
    tapped: bool = False


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
        select(
            i.c.id,
            i.c.received_at,
            i.c.text,
            i.c.processed_at,
            i.c.kind,
            i.c.payload,
            i.c.reply_to,
            i.c.media,
        )  # fmt: skip
        .where(i.c.conversation_id == conversation_id)
        .order_by(i.c.received_at.desc())
        .limit(limit)
    ).all()
    outs = conn.execute(
        select(
            o.c.id,
            o.c.created_at,
            o.c.message,
            o.c.turn_id,
            o.c.author,
            o.c.author_actor_id,
            o.c.status,
            o.c.delivered_at,
            o.c.read_at,
            o.c.channel_message_id,
        )
        .where(o.c.conversation_id == conversation_id)
        .order_by(o.c.created_at.desc())
        .limit(limit)
    ).all()
    ours = {r.channel_message_id: f"out:{r.id}" for r in outs if r.channel_message_id}
    lines = [
        Line(id=f"in:{r.id}", at=r.received_at, direction="in", text=r.text, turn_id=None,
             author="person", reply_to=ours.get(r.reply_to) if r.reply_to else None,
             **_theirs(r.kind, r.payload, r.media))
        for r in ins
    ]  # fmt: skip
    words = _template_words(conn, t, head.channel, outs)
    lines += [
        Line(
            id=f"out:{r.id}",
            at=r.created_at,
            direction="out",
            turn_id=r.turn_id,
            author=r.author,
            author_id=r.author_actor_id,
            status=_delivery(r),
            **(_said(r.message or {}, words) | _parts(r.message or {}, head.channel)),
        )
        for r in outs
    ]
    lines.sort(key=lambda line: line.at)
    if not show_text:  # the parts carry what was said, so they go with the text
        hidden = {"text": None, "image": None, "location": None, "choices": None, "contact": None}
        lines = [line.model_copy(update=hidden) for line in lines]

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


# --- templates -------------------------------------------------------------------------------


class TemplateRow(BaseModel):
    channel: str
    name: str
    language: str
    status: str | None
    category: str | None
    upcoming_category: str | None  # announced change, e.g. to MARKETING
    quality: str | None
    reason: str | None
    status_at: datetime | None
    held: int  # messages waiting for it to become sendable again


def templates(
    conn: Connection, t: RuntimeTables, who: Principal, *, visibility: Visibility = NEED_TO_KNOW
) -> list[TemplateRow]:
    """Our message templates as the channel last described them, with held messages."""
    _need(who, visibility.operations)
    tp, o = t.templates, t.outbox
    held = (
        select(func.count())
        .where(
            o.c.status == "held",
            o.c.channel == tp.c.channel,
            o.c.message["template"].astext == tp.c.name,
            o.c.message["language"].astext == tp.c.language,
        )
        .scalar_subquery()
    )
    rows = conn.execute(select(tp, held.label("held")).order_by(tp.c.name, tp.c.language))
    return [
        TemplateRow(
            channel=r.channel, name=r.name, language=r.language, status=r.status,
            category=r.category, upcoming_category=r.upcoming_category, quality=r.quality,
            reason=r.reason, status_at=r.status_at, held=r.held,
        )
        for r in rows
    ]  # fmt: skip


# --- endpoints: our own numbers and addresses -------------------------------------------------


class EndpointCheck(BaseModel):
    name: str
    level: str  # ok | warn | fail
    detail: str


class EndpointRow(BaseModel):
    channel: str
    address: str  # scheme:value (wa-pnid:<id>, mailto:<address>)
    display: str | None
    name: str | None
    status: str | None
    quality: str | None
    limit: str | None
    health: str | None  # the worst check; None: nothing reported yet
    checks: list[EndpointCheck]
    reported_at: datetime | None


def endpoints(
    conn: Connection, t: RuntimeTables, who: Principal, *, visibility: Visibility = NEED_TO_KNOW
) -> list[EndpointRow]:
    """Our numbers and email addresses with their health, as last reported."""
    from meydan.runtime.endpoints import endpoints as stored

    _need(who, visibility.operations)
    return [
        EndpointRow(
            channel=e.channel, address=str(e.address), display=e.display, name=e.name,
            status=e.status, quality=e.quality, limit=e.limit, health=e.health,
            checks=[EndpointCheck(name=c.name, level=c.level, detail=c.detail) for c in e.checks],
            reported_at=e.reported_at,
        )
        for e in stored(conn, t)
    ]  # fmt: skip


# --- interactions: what came of the messages we sent ----------------------------------------


class Answer(BaseModel):
    at: datetime  # when they sent it (the channel's clock)
    kind: str  # button | interactive | text | ...
    text: str | None  # a button's title; free text only for people who may read messages


class Interaction(BaseModel):
    outbox_id: uuid.UUID
    ref: str | None  # the app's key (Outbound.ref), e.g. "invite:<id>"
    conversation_id: uuid.UUID
    template: str | None
    to: str | None  # in full, masked or None, per visibility
    status: str  # pending | held | sent | delivered | read | failed | dead
    error: str | None
    queued_at: datetime
    sent_at: datetime | None
    delivered_at: datetime | None
    read_at: datetime | None  # "seen": WhatsApp read, email opened
    answers: list[Answer]  # explicit answers: they point at this message
    # The ordinal scale (observe.engagement): 0 none, 1 delivered, 2 seen, 3 answered.
    answered_at: datetime | None  # the first answer, explicit or implicit (database clock)
    answer: Literal["explicit", "implicit"] | None
    engagement: int | None  # None: not sent yet, or undeliverable (not on the scale)
    undeliverable: bool  # the channel couldn't deliver it: the system's failure, not theirs
    seen_bias: str | None  # how this channel's "seen" leans: exact | under | over


ANSWER_BUTTONS = ("button", "interactive")  # their text is our own button title


def interactions(
    conn: Connection,
    t: RuntimeTables,
    who: Principal,
    *,
    ref: str | None = None,
    ref_prefix: str | None = None,
    template: str | None = None,
    conversation_id: uuid.UUID | None = None,
    turn_id: uuid.UUID | None = None,
    within: timedelta | None = None,
    limit: int = 50,
    visibility: Visibility = NEED_TO_KNOW,
) -> list[Interaction]:
    """Messages we sent and what came of them: queued, sent, delivered, seen, answered, and the
    answers that point at them (button presses, quoted replies). Any message: a template by the
    app's `ref` (exact or prefix, e.g. "invite:") or name, or an agent's replies by conversation
    or turn; newest first. `within` counts only what happened that long after sending (a label
    horizon). An implicit answer is the person writing before our next message (whole turn)."""
    _need(who, visibility.operations)
    o, i = t.outbox, t.inbox
    later = o.alias("later")
    next_out = (  # when our next message (another turn) went out in the same conversation
        select(func.min(later.c.sent_at))
        .where(
            later.c.conversation_id == o.c.conversation_id,
            later.c.sent_at > o.c.sent_at,
            later.c.turn_id.is_distinct_from(o.c.turn_id),
        )
        .scalar_subquery()
    )
    explicit = (
        select(func.min(i.c.received_at))
        .where(i.c.channel == o.c.channel, i.c.reply_to == o.c.channel_message_id)
        .scalar_subquery()
    )
    implicit = (
        select(func.min(i.c.received_at))
        .where(
            i.c.conversation_id == o.c.conversation_id,
            i.c.received_at > o.c.sent_at,
            or_(next_out.is_(None), i.c.received_at < next_out),
        )
        .scalar_subquery()
    )
    q = (
        select(o, explicit.label("explicit_at"), implicit.label("implicit_at"))
        .order_by(o.c.created_at.desc(), o.c.id.desc())
        .limit(limit)
    )
    if ref is not None:
        q = q.where(o.c.ref == ref)
    if ref_prefix is not None:
        q = q.where(o.c.ref.startswith(ref_prefix, autoescape=True))
    if template is not None:
        q = q.where(o.c.message["template"].astext == template)
    if conversation_id is not None:
        q = q.where(o.c.conversation_id == conversation_id)
    if turn_id is not None:
        q = q.where(o.c.turn_id == turn_id)
    sent = conn.execute(q).all()
    ids = [r.channel_message_id for r in sent if r.channel_message_id]
    answers: dict[str, list[Answer]] = {}
    read_text = visibility.sees(who, "message_text")
    shown_text = False
    if ids:
        for a in conn.execute(
            select(i.c.reply_to, i.c.sent_at, i.c.kind, i.c.text)
            .where(i.c.channel.in_({r.channel for r in sent}), i.c.reply_to.in_(ids))
            .order_by(i.c.sent_at, i.c.id)
        ):
            text_ok = a.kind in ANSWER_BUTTONS or read_text
            shown_text |= text_ok and a.kind not in ANSWER_BUTTONS
            answers.setdefault(a.reply_to, []).append(
                Answer(at=a.sent_at, kind=a.kind, text=a.text if text_ok else None)
            )
    if shown_text:
        _audit(conn, t, who, visibility.message_text, "read:messages")
    if sent:
        _audit(conn, t, who, visibility.addresses, "read:addresses")
    out = []
    for r in sent:
        answered_at = r.explicit_at or r.implicit_at
        undeliverable = r.status in ("failed", "dead") and r.delivered_at is None
        level = None if undeliverable else engagement(
            sent_at=r.sent_at, delivered_at=r.delivered_at, seen_at=r.read_at,
            answered_at=answered_at, within=within,
        )  # fmt: skip
        out.append(
            Interaction(
                outbox_id=r.id, ref=r.ref, conversation_id=r.conversation_id,
                template=r.message.get("template"), to=_shown(who, visibility.addresses, r.to),
                status=r.status, error=r.last_error, queued_at=r.created_at, sent_at=r.sent_at,
                delivered_at=r.delivered_at, read_at=r.read_at,
                answers=answers.get(r.channel_message_id or "", []),
                answered_at=answered_at,
                answer="explicit" if r.explicit_at else "implicit" if r.implicit_at else None,
                engagement=None if level is None else int(level),
                undeliverable=undeliverable, seen_bias=SEEN_BIAS.get(r.channel),
            )
        )  # fmt: skip
    return out


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
    "TemplateRow",
    "conversation",
    "conversations",
    "costs",
    "mask",
    "overview",
    "problems",
    "templates",
]
