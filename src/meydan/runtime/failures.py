"""Why a turn failed, and what that means for it.

A turn fails for reasons of four kinds, and each asks for something different:

    transient   the model's provider is down, slow or rate limited: try again later, as long as
                the channel still lets us answer (WhatsApp: 24 hours), then give up
    retry       nobody knows yet (an error nothing names; a model that kept answering out of
                shape): a few more tries, then give up
    human       trying again cannot help and someone has to act (the account's credits ran out):
                stop now, so the app can tell someone
    permanent   this turn will not work (the provider refused the content, the history is too
                long for the model): give up now

`classify` names the failure and its group. It reads Pydantic AI's errors, which already say
what happened (`ModelQuotaExceededError`, `ModelRateLimitError`, ...), and an app's own errors,
which say it by their base class:

    class PaymentsDown(TransientError): ...          # tried again later
    class NoSuchRecord(PermanentError): ...          # given up at once

The kind is `error.kind` if the app sets one, else the class name in snake case
(`payments_down`). What the person is told, if anything, is the app's (`on_delay`, `on_failure`).
"""

import re
from dataclasses import dataclass
from typing import Literal

Group = Literal["transient", "retry", "human", "permanent"]
# The most hopeful first: when several models failed (a fallback chain), the turn is worth what
# the most hopeful of them says.
_HOPE: tuple[Group, ...] = ("transient", "retry", "human", "permanent")


class TransientError(Exception):
    """An app's failure that may pass: the turn is tried again later."""


class NeedsHumanError(Exception):
    """An app's failure someone has to act on: the turn stops now."""


class PermanentError(Exception):
    """An app's failure that will not pass: the turn is given up now."""


@dataclass(frozen=True)
class Failure:
    """One failed attempt at a turn, as the hooks see it."""

    kind: str  # what happened: "quota_exhausted", "model_unavailable", an app's own name
    group: Group  # what that means: see the module docstring
    error: BaseException
    attempts: int  # attempts at this turn so far, this one included
    final: bool  # True: the turn is given up and will not be tried again
    retry_after: float | None = None  # seconds the provider asked us to wait, if it said


def _known() -> tuple[tuple[type[BaseException], str, Group], ...]:
    """Pydantic AI's errors, most specific first: subclasses before what they extend. An app
    without the `llm` extra has no Pydantic AI, and only a timeout to name."""
    try:
        from pydantic_ai import exceptions as ai
    except ImportError:
        return ((TimeoutError, "timeout", "transient"),)
    return (
        (ai.ModelQuotaExceededError, "quota_exhausted", "human"),
        (ai.ModelRateLimitError, "rate_limited", "transient"),
        (ai.ModelContextWindowExceededError, "context_too_long", "permanent"),
        (ai.ModelRequestTimeout, "timeout", "transient"),
        (ai.ModelUnavailableError, "model_unavailable", "transient"),  # also overloaded
        (ai.ModelConnectionError, "model_unavailable", "transient"),  # also a transport timeout
        (ai.ModelServerError, "model_unavailable", "transient"),
        (ai.ModelHTTPError, "model_rejected", "permanent"),  # any other 4xx
        (ai.ContentFilterError, "model_refused", "permanent"),
        (ai.UnexpectedModelBehavior, "model_misbehaved", "retry"),
        (ai.ConcurrencyLimitExceeded, "busy", "transient"),
        (ai.UsageLimitExceeded, "usage_limit", "permanent"),
        (ai.RunTimedOut, "timeout", "transient"),
        (TimeoutError, "timeout", "transient"),
    )


_KNOWN = _known()


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def classify(error: BaseException) -> tuple[str, Group, float | None]:
    """The kind of a failure, its group, and how long the provider asked us to wait."""
    if isinstance(error, BaseExceptionGroup):  # every model of a fallback chain failed
        named = [classify(e) for e in error.exceptions]
        return min(named, key=lambda n: _HOPE.index(n[1]))
    for base, group in (
        (TransientError, "transient"),
        (NeedsHumanError, "human"),
        (PermanentError, "permanent"),
    ):
        if isinstance(error, base):
            return getattr(error, "kind", None) or _snake(type(error).__name__), group, None
    for cls, kind, group in _KNOWN:
        if isinstance(error, cls):
            return kind, group, getattr(error, "retry_after", None)
    return "error", "retry", None
