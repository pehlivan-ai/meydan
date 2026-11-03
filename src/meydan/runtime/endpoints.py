"""Our own addresses: the numbers we send from and the email addresses people write to.

An app has a handful of them, each with a life the channel controls: a WhatsApp number is added,
verified, registered, gets its display name reviewed, a quality rating and a messaging limit,
and can be flagged or banned; an email address works only while its domain's DNS (MX, SPF, DKIM,
DMARC) says so. meydan keeps the latest state of each, as its source last reported it, and every
source states its verdict as checks (`details["checks"]`: name, level ok|warn|fail, detail), so
`health` reads the same way for every channel.

Sources: `whatsapp.WhatsAppCloud.list_numbers` (a sync: at startup, periodically, or when a number
webhook arrives) and `email.dns_health` (a DNS check). The CLI (`python -m meydan.cli`) adds,
verifies and registers numbers; nothing here sends messages.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from sqlalchemy import Connection, Engine, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from meydan.channels.base import Address, EndpointUpdate
from meydan.runtime.tables import RuntimeTables

Level = Literal["ok", "warn", "fail"]
LEVELS: tuple[Level, ...] = ("ok", "warn", "fail")  # in order of worse


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "level": self.level, "detail": self.detail}


def checks_of(details: dict[str, Any]) -> list[Check]:
    return [
        Check(c["name"], c["level"], c.get("detail", ""))
        for c in details.get("checks", [])
        if c.get("level") in LEVELS
    ]


def worst(checks: Iterable[Check]) -> Level | None:
    """The worst level among the checks; None if there are none (nothing is known yet)."""
    levels = [LEVELS.index(c.level) for c in checks]
    return LEVELS[max(levels)] if levels else None


@dataclass(frozen=True)
class Endpoint:
    channel: str
    address: Address
    account: str | None
    display: str | None
    name: str | None
    status: str | None
    quality: str | None
    limit: str | None
    details: dict[str, Any]
    reported_at: datetime | None

    @property
    def checks(self) -> list[Check]:
        return checks_of(self.details)

    @property
    def health(self) -> Level | None:
        return worst(self.checks)

    @classmethod
    def of(cls, row: Any) -> "Endpoint":
        return cls(
            channel=row.channel, address=Address(row.scheme, row.value), account=row.account,
            display=row.display, name=row.name, status=row.status, quality=row.quality,
            limit=row.limit, details=dict(row.details or {}), reported_at=row.reported_at,
        )  # fmt: skip


KEPT = ("account", "display", "name", "status", "quality", "limit")


def record(conn: Connection, t: RuntimeTables, update: EndpointUpdate) -> None:
    """Keep the latest report for this address. An older report than the one kept is ignored
    (syncs and webhooks race); fields a report leaves out (None) keep their value, and its
    details are merged over the kept ones."""
    ep = t.endpoints
    values = {
        "channel": update.channel,
        "scheme": update.address.scheme,
        "value": update.address.value,
        "account": update.account,
        "display": update.display,
        "name": update.name,
        "status": update.status,
        "quality": update.quality,
        "limit": update.limit,
        "details": update.details,
        "reported_at": update.at,
    }
    stmt = pg_insert(ep).values(values)
    new = stmt.excluded
    newer = ep.c.reported_at.is_(None) | (new.reported_at >= ep.c.reported_at)

    def latest(column: str):
        kept = getattr(ep.c, column)
        return func.coalesce(getattr(new, column), kept)

    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[ep.c.scheme, ep.c.value],
            where=newer,
            set_={
                **{
                    c: latest(c)
                    for c in ("account", "display", "name", "status", "quality", "limit")
                },
                "details": ep.c.details.op("||")(new.details),
                "reported_at": new.reported_at,
                "updated_at": func.now(),
            },
        )
    )


def record_all(conn: Connection, t: RuntimeTables, updates: Iterable[EndpointUpdate]) -> int:
    n = 0
    for u in updates:
        record(conn, t, u)
        n += 1
    return n


def endpoints(conn: Connection, t: RuntimeTables, channel: str | None = None) -> list[Endpoint]:
    ep = t.endpoints
    q = select(ep).order_by(ep.c.channel, ep.c.display, ep.c.value)
    if channel is not None:
        q = q.where(ep.c.channel == channel)
    return [Endpoint.of(r) for r in conn.execute(q)]


def get(conn: Connection, t: RuntimeTables, address: Address) -> Endpoint | None:
    ep = t.endpoints
    row = conn.execute(
        select(ep).where(ep.c.scheme == address.scheme, ep.c.value == address.value)
    ).first()
    return Endpoint.of(row) if row else None


class NumberSource(Protocol):
    async def list_numbers(self, account: str) -> list[EndpointUpdate]: ...


async def sync(engine: Engine, t: RuntimeTables, channel: NumberSource, account: str) -> int:
    """Read every number of `account` from the channel and record it. Returns how many."""
    updates = await channel.list_numbers(account)
    with engine.begin() as conn:
        return record_all(conn, t, updates)
