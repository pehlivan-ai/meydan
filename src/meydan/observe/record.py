"""Writes a turn's record: one row per turn, one per model call (the runtime calls this)."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Connection, func, insert

from meydan.observe.context import TurnContext
from meydan.runtime.tables import RuntimeTables


def _cost_source(sources: set[str | None]) -> str | None:
    if not sources or sources == {None}:
        return None
    if len(sources) == 1:
        return next(iter(sources))
    return "mixed"  # some exact, some estimated or unknown


def write_turn(
    conn: Connection,
    t: RuntimeTables,
    ctx: TurnContext,
    *,
    channel: str,
    started_at: datetime,
    status: str,
    messages_in: int,
    messages_out: int,
    error: str | None = None,
) -> None:
    usage = ctx.usage
    costs = [u.cost_usd for u in usage if u.cost_usd is not None]
    models = [u.model for u in usage if u.model]
    conn.execute(
        insert(t.turns).values(
            id=ctx.turn_id,
            conversation_id=ctx.conversation_id,
            actor_id=ctx.actor_id,
            channel=channel,
            started_at=started_at,
            finished_at=func.clock_timestamp(),
            status=status,
            error=error[:500] if error else None,
            messages_in=messages_in,
            messages_out=messages_out,
            model_calls=len(usage),
            model=models[-1] if models else None,
            input_tokens=sum(u.input_tokens for u in usage),
            output_tokens=sum(u.output_tokens for u in usage),
            cost_usd=sum(costs, Decimal(0)) if costs else None,
            cost_source=_cost_source({u.cost_source for u in usage}),
        )
    )
    if usage:
        conn.execute(
            insert(t.usage),
            [
                {
                    "turn_id": ctx.turn_id,
                    "provider": u.provider,
                    "model": u.model,
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "cache_read_tokens": u.cache_read_tokens,
                    "cache_write_tokens": u.cache_write_tokens,
                    "reasoning_tokens": u.reasoning_tokens,
                    "cost_usd": u.cost_usd,
                    "cost_source": u.cost_source,
                }
                for u in usage
            ],
        )
