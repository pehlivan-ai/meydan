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

### Engagement: one ordinal scale (since v0.12)
`observe.engagement`: `Engagement` 0 none, 1 delivered, 2 seen, 3 answered, and the pure
function `engagement(sent_at=, delivered_at=, seen_at=, answered_at=, within=)`. `interactions`
gives every message (by `ref`, template, `conversation_id` or `turn_id`) its `engagement`,
`answered_at`, `answer` (`explicit` | `implicit`), `undeliverable` and the channel's `seen_bias`
(`SEEN_BIAS`).

Every message we send, a template or an agent's reply, climbs one scale. The scale is the same on
every channel, so you can compare channels and use a level as a label (for example, feedback on
an agent's replies).

| Level | Meaning | WhatsApp | Email | SMS / push (later) |
|---|---|---|---|---|
| **0** none | We sent it. Nothing came back. | — | — | — |
| **1** delivered | It reached their device or mailbox. | `delivered` | accepted by their server | carrier delivery report / push delivered |
| **2** seen | They saw it. | `read` | opened (tracking pixel) | — / push opened |
| **3** answered | They answered it. | a button or a quoted reply (`context.id`), or a message before our next one | `In-Reply-To` / `References`, or a message before our next one | a message before our next one |

```python
from datetime import timedelta
from meydan.observe.query import interactions

for m in interactions(conn, tables, who, turn_id=turn_id, within=timedelta(hours=24)):
    m.engagement, m.answer, m.seen_bias  # 0..3, "explicit" | "implicit" | None, "under" | "over"
```

- Each level includes the levels below it. An answer counts as seen without a read receipt.
- meydan stores no level. It calculates the level from the times it keeps, so you can ask for
  any horizon later (`within=`).
- An answer that points at the message (a button, a quote, an email reply) is explicit. A message
  from the person before our next message is implicit, and it belongs to the whole reply.
- A message that the channel cannot deliver has no level (`undeliverable=True`). It is a failure
  of the system, not a behaviour of the person.
- "Seen" leans differently per channel (`seen_bias`). WhatsApp read receipts can be off
  (`under`: missing, never false). Email opens fire on image prefetch (`over`: can be false).
- The scale says how far a message got, not what the answer said.

## Not here (yet)
- Deep traces (spans per model call and tool call): an optional OpenTelemetry export (planned).
- Message text never goes into the observe tables; it stays in the inbox/outbox.
