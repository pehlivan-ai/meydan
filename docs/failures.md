# Failures: put off, tried again, or given up

Available in `meydan.runtime` (`failures`).

A turn fails for reasons of four kinds, and each asks for something different. The runtime
names the failure (`classify`), decides what happens to the turn, and leaves to the app only
what the person is told.

| Group | Example | What happens to the turn |
|---|---|---|
| `transient` | the model's provider is down, slow or rate limited | put off: tried again after 30 s, 2 min, 10 min, 30 min, then hourly, as long as the channel still lets us answer (`reply_window` in `meydan.rules`; 24 h on WhatsApp), then given up. A new message from the person tries it at once |
| `retry` | an error nothing names; a model that kept answering out of shape | tried `max_turn_attempts` times (3), a few seconds apart, then given up |
| `human` | the account's credits ran out | given up at once: retrying cannot help, someone has to act |
| `permanent` | the provider refused the content; the history is too long | given up at once |

Only `retry` failures count toward `max_turn_attempts`: an hour of a provider being down is
not a poison message.

## Kinds meydan knows

From Pydantic AI's errors, which already say what happened: `quota_exhausted`, `rate_limited`
(the wait the provider asks for is kept), `model_unavailable`, `timeout`, `busy`,
`context_too_long`, `model_rejected`, `model_refused`, `model_misbehaved`, `usage_limit`;
`error` for anything else. When every model of a fallback chain failed, the most hopeful one
decides.

## Your own

Derive from the group you mean; the kind is `kind` if you set one, else the class name in
snake case:

```python
from meydan.runtime.failures import NeedsHumanError, TransientError


class PaymentsDownError(TransientError): ...  # put off, like a provider that is down


class AccountClosedError(NeedsHumanError):
    kind = "account_closed"
```

## What the person is told

Nothing, unless you say. Two hooks, each given the turn and a `Failure` (`kind`, `group`,
`error`, `attempts`, `final`, `retry_after`):

```python
def late(turn, failure):  # the first time a turn is put off; not again at later tries
    if failure.kind == "model_unavailable":
        return [Outbound.text_message("Biraz gecikiyoruz, birazdan döneceğiz.")]
    return []


def gave_up(turn, failure):  # the turn will not be tried again
    if failure.kind == "quota_exhausted":
        return [Outbound.text_message("Kısa bir süre hizmet veremiyoruz.")]
    return []


runtime = Runtime(..., on_delay=late, on_failure=gave_up)
```

Telling someone inside (an operator, the owner) is not a hook: every failure is written down
(`inbox.error`, the turn record) and `meydan.observe` reads it (`problems`).
