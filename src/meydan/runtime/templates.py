"""Message templates as the channel describes them: review, pauses, category and quality.

The channel reports changes (WhatsApp: template webhooks) and can be asked for the full list (a
sync at startup, so a missed webhook doesn't matter). meydan keeps the latest state per template
and the sender asks it before sending one:

- sendable (APPROVED) or unknown: send;
- waiting (PENDING, PAUSED, IN_APPEAL, LIMIT_EXCEEDED): hold the message, out of the
  conversation's line, and send it when the template is sendable again;
- gone (REJECTED, DISABLED, DELETED, ...): give up with the reason.

That is the channel's own advice for paused templates: halt, and resume when they're active.
"""

from collections.abc import Iterable
from typing import Any, Literal

from sqlalchemy import Connection, Engine, and_, case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from meydan.channels.base import Outbound, TemplateUpdate
from meydan.runtime.tables import RuntimeTables

SENDABLE = frozenset({"APPROVED", "REINSTATED"})
WAITING = frozenset({"PENDING", "PAUSED", "IN_APPEAL", "LIMIT_EXCEEDED"})

Verdict = Literal["send", "hold", "fail"]


def record(conn: Connection, t: RuntimeTables, update_: TemplateUpdate) -> None:
    """Apply one update. A status older than the one we keep is ignored (channels retry and
    deliver out of order); other fields are filled in from whatever arrives."""
    tp = t.templates
    status = "APPROVED" if update_.status == "REINSTATED" else update_.status
    values = {
        "channel": update_.channel,
        "account": update_.account,
        "name": update_.name,
        "language": update_.language,
        "external_id": update_.external_id,
        "status": status,
        "category": update_.category,
        "upcoming_category": update_.upcoming_category,
        "quality": update_.quality,
        "reason": update_.reason,
        "status_at": update_.at if status else None,
    }
    stmt = pg_insert(tp).values(values)
    new = stmt.excluded
    newer = (tp.c.status_at.is_(None)) | (new.status_at >= tp.c.status_at)
    keep_status = new.status.is_(None) | ~newer

    def latest(column: str):
        return func.coalesce(getattr(new, column), getattr(tp.c, column))

    stmt = stmt.on_conflict_do_update(
        index_elements=[tp.c.channel, tp.c.name, tp.c.language],
        set_={
            "account": func.coalesce(func.nullif(new.account, ""), tp.c.account),
            "external_id": latest("external_id"),
            "status": _case(keep_status, tp.c.status, new.status),
            "status_at": _case(keep_status, tp.c.status_at, new.status_at),
            "reason": _case(new.status.is_(None), tp.c.reason, new.reason),
            "category": latest("category"),
            # an announced change is over once the category itself changes
            "upcoming_category": _case(
                new.category.is_not(None) & new.upcoming_category.is_(None),
                None,
                latest("upcoming_category"),
            ),
            "quality": latest("quality"),
            "updated_at": func.now(),
        },
    )
    conn.execute(stmt)
    if status in SENDABLE:
        release(conn, t, update_.channel, update_.name, update_.language)
    elif status and status not in WAITING:
        give_up(conn, t, update_.channel, update_.name, update_.language, status, update_.reason)


def _case(condition, when_true, when_false):
    return case((condition, when_true), else_=when_false)


def record_all(conn: Connection, t: RuntimeTables, updates: Iterable[TemplateUpdate]) -> int:
    n = 0
    for u in updates:
        record(conn, t, u)
        n += 1
    return n


def verdict(
    conn: Connection, t: RuntimeTables, channel: str, message: Outbound
) -> tuple[Verdict, str | None]:
    """Send, hold or fail this template message, with the template's status as the reason."""
    tp = t.templates
    row = conn.execute(
        select(tp.c.status, tp.c.reason).where(
            tp.c.channel == channel,
            tp.c.name == message.template,
            tp.c.language == message.language,
        )
    ).first()
    if row is None or row.status is None or row.status in SENDABLE:
        return "send", None
    reason = f"template {message.template} ({message.language}) is {row.status}"
    if row.reason and row.reason != "NONE":
        reason += f": {row.reason}"
    return ("hold" if row.status in WAITING else "fail"), reason


def _of(t: RuntimeTables, channel: str, name: str, language: str):
    o = t.outbox
    return and_(
        o.c.channel == channel,
        o.c.message["kind"].astext == "template",
        o.c.message["template"].astext == name,
        o.c.message["language"].astext == language,
    )


def release(conn: Connection, t: RuntimeTables, channel: str, name: str, language: str) -> int:
    """Held messages of a template that is sendable again go back in line, due now. They keep
    their place: being older, they go out before what was queued after them."""
    o = t.outbox
    return conn.execute(
        update(o)
        .where(o.c.status == "held", _of(t, channel, name, language))
        .values(status="pending", next_attempt_at=func.clock_timestamp(), updated_at=func.now())
    ).rowcount


def give_up(
    conn: Connection,
    t: RuntimeTables,
    channel: str,
    name: str,
    language: str,
    status: str,
    reason: str | None,
) -> int:
    """Held messages of a template that won't come back (rejected, disabled, deleted)."""
    o = t.outbox
    why = f"template {name} ({language}) is {status}" + (f": {reason}" if reason else "")
    return conn.execute(
        update(o)
        .where(o.c.status == "held", _of(t, channel, name, language))
        .values(status="dead", last_error=why, updated_at=func.now())
    ).rowcount


async def sync(engine: Engine, t: RuntimeTables, channel: Any, account: str) -> int:
    """Read every template of `account` from the channel and record it (e.g. at startup, so a
    webhook missed while the app was down doesn't matter). Returns how many were recorded."""
    updates = await channel.list_templates(account)
    with engine.begin() as conn:
        return record_all(conn, t, updates)
