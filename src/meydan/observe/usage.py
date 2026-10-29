"""Model usage and cost per model call.

Cost: the provider's own figure when it reports one (OpenRouter: what was billed), else an
estimate from the model's published prices (genai-prices, via Pydantic AI), else unknown. Each row
says which (`cost_source`), so reports can show how much of a total is exact.

Read from Pydantic AI's `ModelResponse` by its fields only: any agent framework can build `Usage`
rows itself and pass them to `record_usage`.
"""

from collections.abc import Iterable
from dataclasses import KW_ONLY, dataclass
from decimal import Decimal
from typing import Any

from meydan.observe.context import record_usage


@dataclass(frozen=True)
class Usage:
    provider: str | None
    model: str | None
    _: KW_ONLY  # numbers by name only: a cost can't slip into a token count
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: Decimal | None = None
    cost_source: str | None = None  # "provider" | "estimate" | None (unknown)


def _cost(response: Any) -> tuple[Decimal | None, str | None]:
    details = getattr(response, "provider_details", None) or {}
    if details.get("cost") is not None:
        return Decimal(str(details["cost"])), "provider"
    usage = getattr(response, "usage", None)
    if usage is not None and getattr(usage, "cost", None) is not None:
        return Decimal(str(usage.cost)), "estimate"
    return None, None


def usage_from_messages(messages: Iterable[Any]) -> list[Usage]:
    """One `Usage` per model response in a list of Pydantic AI messages (requests are skipped)."""
    out = []
    for m in messages:
        if getattr(m, "kind", None) != "response":
            continue
        u = getattr(m, "usage", None)
        cost, source = _cost(m)
        out.append(
            Usage(
                provider=getattr(m, "provider_name", None),
                model=getattr(m, "model_name", None),
                input_tokens=getattr(u, "input_tokens", 0) or 0,
                output_tokens=getattr(u, "output_tokens", 0) or 0,
                cache_read_tokens=getattr(u, "cache_read_tokens", 0) or 0,
                cache_write_tokens=getattr(u, "cache_write_tokens", 0) or 0,
                reasoning_tokens=getattr(u, "output_reasoning_tokens", 0) or 0,
                cost_usd=cost,
                cost_source=source,
            )
        )
    return out


def record_run(result: Any) -> list[Usage]:
    """Record the model calls of one agent run (its new messages) on the current turn."""
    usage = usage_from_messages(result.new_messages())
    record_usage(*usage)
    return usage
