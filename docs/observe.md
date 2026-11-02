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
Read models in `meydan.observe.query`, each taking the **principal first** and applying your
`Visibility` inside the query (there is no unscoped variant; [visibility.md](visibility.md)).
Item names below; the default preset `need_to_know()` maps them to the scopes in brackets:

| Read model | Returns | Visibility items |
|---|---|---|
| `overview` | turns (replied/failed/gave up), cost and its exact share, average/slowest turn, active conversations, pending sends, dead letters | `operations` (`ops`); cost needs `costs` |
| `conversations` | most recent first: who, address, channel, messages, turns, cost | `all_conversations` (else only your own), `addresses` (else masked), `costs` |
| `conversation` | transcript + each turn's timeline (received → seen → typing → sent), model, tokens, cost | `message_text` (others' text, audited), `agent_details` (the turns), `costs` |
| `costs` | per day and per model | `costs` (`costs:read`) |
| `problems` | failed turns and dead letters with their errors | `operations` (`ops`) |
| `interactions` (v0.11) | messages we sent and what came of them: queued → sent → delivered → read, and the answers that point at them (button presses, quoted replies), by the app's `ref` (exact or prefix) or template | `operations` (`ops`); `addresses` (else masked); free-text answers need `message_text` (audited), button titles are always shown (they're our own words) |

HTTP (extra `api`), next to `meydan.auth.web`:

```python
from meydan.observe.web import routes as observe_routes

app.include_router(observe_routes(auth, visibility), prefix="/observe")  # 401 / 403 / 404
```

### Following a message we sent (since v0.11)
Give the message a key of your own when you send it, and every later fact about it is found by
that key, with no table of your own to keep in step:

```python
Outbound.template_message("invite", "en", buttons=(...), ref=f"invite:{invite_id}")
```

- The outbox keeps `ref`, `sent_at`, and the receipt times `delivered_at` / `read_at` (the
  channel's clock, the first receipt that showed it; receipts arrive in any order, and a read
  counts as a delivery).
- An answer carries the id of the message it answers (WhatsApp `context.id`) in
  `Inbound.reply_to`, stored on the inbox row. `replied_to(conn, tables, message)` gives an
  interceptor the outbox row and `ref` it answers, so it can match by what was answered.
- `interactions(conn, tables, who, ref=...)` (or `ref_prefix="invite:"`, `template="invite"`)
  reads it all back for a screen.

Meaning stays in the app: meydan says "delivered at, read at, answered with this button"; what an
answer does (a role, a registration) is the app's interceptor.

## Not here (yet)
- Deep traces (spans per model call and tool call): an optional OpenTelemetry export (planned).
- Message text never goes into the observe tables; it stays in the inbox/outbox.
