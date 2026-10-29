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

## Reading it (since v0.5.0)
Read models in `meydan.observe.query`, each taking the **principal first** and enforcing its
scopes inside the query (there is no unscoped variant):

| Read model | Returns | Needs |
|---|---|---|
| `overview` | turns (replied/failed/gave up), cost and its exact share, average/slowest turn, active conversations, pending sends, dead letters | `ops` |
| `conversations` | most recent first: who (address masked), channel, messages, turns, cost | `conversations:read:all`, else only your own |
| `conversation` | transcript + each turn's timeline (received → seen → typing → sent), model, tokens, cost | others' message text needs `messages:read` (else redacted); each such read is written to `meydan_audit` |
| `costs` | per day and per model | `costs:read` |
| `problems` | failed turns and dead letters with their errors | `ops` |

HTTP (extra `api`), next to `meydan.auth.web`:

```python
from meydan.observe.web import routes as observe_routes

app.include_router(observe_routes(auth), prefix="/observe")  # 401 / 403 / 404 as appropriate
```

## Not here (yet)
- Deep traces (spans per model call and tool call): an optional OpenTelemetry export (planned).
- Message text never goes into the observe tables; it stays in the inbox/outbox.
