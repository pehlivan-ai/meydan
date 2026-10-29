# Observe: what each turn did, how long it took, what it cost

Available since v0.4.0 (`meydan.observe`).

Every turn the runtime runs leaves one record, written in the **same transaction** as the reply
(no reply without its record, no record without its reply). Failed turns are recorded too: the
model calls they made still cost money.

| Table | One row per | Holds |
|---|---|---|
| `meydan_turns` | turn (one attempt to answer a burst) | conversation, actor, channel, started/finished, status (`replied`, `failed`, `gave_up`), error, messages in/out, model calls, last model, tokens, cost |
| `meydan_usage` | model call within a turn | provider, model, tokens (input, output, cache, reasoning), cost and where the cost came from |

The reply's outbox rows carry the turn's id (`turn_id`), and the inbox carries the presence
timeline (`received_at`, `read_at`, `typing_at`): a turn's whole story is one join.

## Reporting model usage
One line in your agent code, inside the turn:

```python
from meydan import observe

result = await agent.run(...)
observe.record_run(result)  # Pydantic AI: one usage row per model response of this run
```

Other frameworks build `observe.Usage(...)` rows and call `observe.record_usage(...)`. Outside a
turn (scripts, tests) both do nothing.

## Cost
Per model call, in USD:
1. **provider**: the provider's own figure when it reports one (OpenRouter: what was billed);
2. **estimate**: else an estimate from the model's published prices (genai-prices, via Pydantic AI);
3. unknown otherwise.

A turn's `cost_source` is `provider`, `estimate` or `mixed`, so a report can say how much of a total
is exact.

## Not here (yet)
- Reading it: principal-scoped read models and HTTP endpoints (planned).
- Deep traces (spans per model call and tool call): an optional OpenTelemetry export (planned).
- Message text never goes into these tables; it stays in the inbox/outbox.
