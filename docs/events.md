# Events: facts and their subscribers

Available since v0.47.0 (`meydan.events`; extra `runtime`).

Something happened (a message arrived, a photo was kept, a payment came in) and several parts of
the app care, each in its own way and at its own pace: mark it seen, notify the team, tell a
partner. `meydan.events` is the one place for that: a fact is written once, where it happened, and
every subscriber that wants it gets its own copy to handle, retried until it is done.

```python
from meydan.events import Events, Later, PermanentError, Subscriber, emit


async def tell_the_team(event):  # at least once: make it idempotent
    ...  # return None (done) or Later(seconds); raise to retry


events = Events(engine, tables, [Subscriber("team", ("message.ingested",), tell_the_team)])
await events.run(stop)  # the runtime runs its own (Runtime.events)

with engine.begin() as conn:  # where it happened, in that transaction
    emit(conn, tables, "payment.paid", key=ref, data={"amount": 1500})
```

With the runtime, pass your subscribers to it: `Runtime(..., subscribers=[...])`; its loop runs
them with its own (`Runtime.run`), and `await runtime.events_once()` runs one pass in tests.

## What it promises

| Promise | How |
|---|---|
| A fact exists only if the change it belongs to committed | `emit` writes in the caller's transaction |
| Every matching subscriber gets it, once each | one delivery row per subscription, `UNIQUE (subscriber, event_id)`, written in that same statement |
| A late commit is never skipped, a long transaction never stalls anyone | there is no shared log read by position: each subscriber claims its own rows by state |
| One slow or failing subscriber never holds up another | separate rows, separate claims |
| Nothing is lost when a handler fails or a process dies | failures retried with backoff (15 s + 2^n, up to an hour, +10% jitter), dead after `max_attempts` with the reason; a claim whose lease ran out is taken back |
| A handler never runs inside a held transaction | the claim commits first; the handler runs after |
| The same fact twice is one fact | `key`: one fact per `(kind, key)` |
| A fact emitted in one process reaches a subscriber in another | subscriptions are rows (`register`, at start and before the first message the runtime receives) |

Delivery is **at least once**: a handler may see a fact again (it acted, then crashed before its
finish was recorded). Make handlers idempotent; `event.delivery_id` is a key for what they do
outside.

## A handler's outcomes

| It | The delivery |
|---|---|
| returns `None` | `done` |
| returns `Later(seconds)` | back to `pending` then, the attempt not counted (not yet: a row a turn holds, a time not reached) |
| raises `PermanentError(reason)` | `dead` now, with the reason |
| raises anything else | `pending` after `backoff(attempts)`, the error kept; `dead` after `max_attempts` |
| runs past `lease_s` | cut off (a timeout: retried); a process that died mid-handler leaves a claim that `rescue` takes back |

`events.retry(conn, tables, delivery_id)` puts a dead one back; `events.unsubscribe(conn, tables,
name)` removes a subscriber you no longer run (its deliveries go with it). `Events.backlog()` and
`Runtime.health()` (`events_s`, `events_dead`) say whether work is waiting.

## Options

`Subscriber(name, kinds, handle, channel=None, delay_s=0, max_attempts=8, lease_s=120)`:
`channel` takes that channel's facts and channel-less ones; `delay_s` holds each delivery until the
fact's time plus that (the runtime's "seen" is a second after arrival). `Events(..., poll_s=0.5,
batch=20, keep_done=7 days, keep_dead=30 days, keep_events=30 days)`: finished deliveries and old
facts are pruned hourly, in batches.

## The runtime's own subscribers (meydan.runtime.ingest)

```
receive ─► inbox row ─┬─ text: ingested now ──────────────────────────► message.ingested
                      └─ a photo: message.received ─► Ingest (download, keep) ─► message.ingested
message.ingested ─► Seen: read_after_s after arrival, the channel's read receipt (each message);
                   not while a person has the conversation (meydan.operator takeover)
conversation.read ─► StaffSeen: someone on the team opened it (operator.mark_read): what is still
                   unread is marked seen. A fact emitted by the ops process, handled by the app's
worker: a turn waits for its messages to be ingested (at most ingest_wait_s, then goes without)
```

"Seen" follows what is in the system, not a timer: a photo is marked seen once it is kept, and a
refused read receipt is recorded and tried again. Neither subscriber ever waits on a message a turn
holds (the turn's transaction is open while the model thinks): they skip it.

## Why this shape

- **Copies, not a shared log read by position.** A log read by `position > last` skips a
  transaction that commits late (a sequence number is taken before commit). The usual fix, reading
  only below the oldest open transaction (`pg_snapshot_xmin`), stalls every reader while any
  transaction is open, and a meydan turn keeps one open while the model thinks. Snapshot diffs (PgQ)
  avoid both but need a ticker. A copy per subscriber, claimed by state, has neither problem.
- **Shapes borrowed**: the claim, commit, handle cycle and the dead letter kept in place (Oban,
  River); the time as a key column of the claim index and the attempt fence on finish (pg-boss);
  emitting inside the caller's transaction (River); copies per subscriber (pg-boss,
  `meydan.notify`).

## Not here yet

LISTEN/NOTIFY to wake the loop at once (polling every `poll_s` is enough for "seen"), strict order
per key, and moving meydan's own queues (bridge events, notify, the access outbox, expectation
sweeps) onto it: each will be its own release.
