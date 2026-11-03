# Expect and ask: waiting for an answer

Available since v0.13.0 (`meydan.runtime.expect`). Research behind it (Inngest waitForEvent,
Trigger.dev waitpoint tokens, Restate awakeables, Temporal signals, Step Functions task tokens,
the durable promise spec, Twilio's Send & Wait for Reply, the EIP Aggregator): a waitpoint is a
stored record with an id and a timeout, completed once from outside, with every outcome explicit.

## The record
`meydan_expectations`: kind (the app's name), channel, the identity that must answer (or none:
anyone), the app's `key` and `data`, the options, the token's hash, `expires_at`, the asking
message's `turn_id`, and the outcome.

```
pending ──► answered (one of its options, by whom, when)
        ├─► expired        (past expires_at: the sweep, or an answer that came too late)
        ├─► cancelled      (cancel(...))
        └─► undeliverable  (the asking message became a dead letter)
```

Each transition happens once (`UPDATE ... WHERE state = 'pending'`, the row locked on answers),
so a webhook delivered twice can't run the app's handler twice.

## Functions
| Function | Does |
|---|---|
| `expect(conn, tables, kind=, channel=, options=, ttl=, identity_id=, data=, key=, created_by=, on_pending=)` | records it; returns `(Expectation, token)`. With a `key`, a live one decides by `on_pending`: `refuse` (PendingError), `resend` (same record, new token and expiry), `allow` |
| `ask(conn, runtime, kind=, channel=, endpoint=, address=, message=, options=, ttl=, ...)` | `plan_contact` + `expect` + the message (`message(buttons)` gets `{option: payload}`), sent with `ref="expect:<id>"` |
| `renew`, `cancel`, `get`, `pending` | a new token and expiry; close as cancelled; read one; the live one for a kind and key |
| `Expectations(tables, {kind: Handler}, replies=, payload_of=)` | `Runtime(expect=...)`: its `intercept` runs before other interceptors; `sweep` runs every `RuntimeConfig.expect_sweep_s` |
| `Handler(on_answer, on_close=, replies=)` | the app's meaning: `on_answer(conn, exp, routed) -> replies`; `on_close(conn, exp)` for expired / undeliverable; `Replies` in the app's language |

Button payloads are `expect:<token>:<option>`; only the token's hash is stored.

## Checks on an answer, in order
1. The token is known and its kind has a handler (else `Replies.unknown`).
2. Not cancelled / already answered / closed (each its own reply).
3. In time (else it's closed as expired, `on_close` runs, `Replies.expired`).
4. The press answers the message that asked, when the channel says which (`reply_to`).
5. The sender owns the identity it was asked of (`Replies.not_yours`).
6. The option is one of its options.

## Not here (yet)
- Groups (first N of many, backfill): an Aggregator over several asks, with the planner.
- Sign-in codes (text, no outbound message) moving onto `expect`: next.
