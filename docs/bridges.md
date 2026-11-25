# Bridges: outside systems talk to an instance

Status: incoming events and expectations completed by an event are **available** (v0.40.0);
the rest of this page is the design they belong to. The README's island "later, bridges connect it to
other islands". A bridge is that link, and the other island may be another meydan instance or a
system that is not meydan at all: a partner's app, a payment provider, a booking tool the
business already uses.

## Why

A meydan app connects people (A with B) and records when a connection reaches an outcome: a
booking confirmed, a registration paid, an order delivered. The systems around those people
already exist and the app should not ask anyone to replace them ("them or us"). It works next
to them: the outside system stays the record of what it owns (payments, a calendar, an order
book), and the meydan app is where people talk and decide. A bridge carries the facts between
the two.

Payment is the first case and shows the shape. Whoever collects the money (a partner, the
app's own payment provider, or a marketplace that splits it), the app needs one fact:
"what was owed under this reference is paid". Who collected it stays behind the bridge, so
changing who collects changes the bridge, not the app.

## Concepts

- **Connection**: one outside system an instance is linked to: its name, its secret (kept
  outside code), what it may do, whether it is on. A connection is a **principal**: it acts only
  within its own scope, like any team member or agent.
- **Event**: one fact the outside system tells us (`payment.succeeded`, `booking.cancelled`),
  with an id of its own, a type, a time and data. Events arrive at least once and in any order.
- **Reference (ref)**: an unguessable token the app hands out (in a link, a message) so that a
  later event can be tied to what it is about without sharing personal data. A ref is the token
  of an **expectation** (`docs/expect.md`): something the app waits for, completed once from
  outside, with an expiry.
- **Operation**: a typed action of the app (`register`, `hold`, `settle`) on behalf of an actor.
  The domain logic lives in operations. An agent's tool, a bridge event and any later door call
  the same operation; none of them carries logic of its own (**operations first**).
- **Outcome**: a recorded result (roadmap 2, the outcome ledger): kind, subject, actor, the
  evidence (the event id), an amount. What "closed" means is the app's choice.

## Where a bridge runs

The core does not know where a bridge runs. The contract is the same:

```
                 ┌──────────────── instance ─────────────────┐
                 │ operations · identity · expect · outcomes  │
                 │                                            │
                 │   bridge contract: event envelope, ref,    │
                 │   identity mapping, principal, audit       │
                 └─────▲───────────────▲──────────────▲───────┘
                       │               │              │
               in-process          over HTTP       through tables
            (a function in      (a separate service, (a worker with a
             the app)            another language,     restricted DB role)
                                 the partner's side,
                                 another instance)
```

## Guarantees (whatever the transport)

| Guarantee | How |
|---|---|
| Only the connection can speak for itself | Signed requests, one secret per connection; [Standard Webhooks](https://www.standardwebhooks.com/) (`webhook-id`, `webhook-timestamp`, `webhook-signature`). A channel's own format (Meta's `X-Hub-Signature-256`) only where that channel dictates it |
| No replays | The timestamp must be recent (default 5 minutes) |
| Once | Events are stored first, unique on (connection, event id); a repeat is acknowledged and ignored |
| Fast answer | Verify, store, 2xx. Handling happens in a worker, as with messages |
| Nothing lost | Raw payload kept; a failing handler is retried with backoff, then dead-lettered with its reason |
| Never on a guess | An outside user id becomes an identity only with proof (a claim with basis `proof`, e.g. the event carries a ref we issued to that person), never by matching a name or an email |
| Recorded | Every event, its handling and every outcome can be read by the team (observe), and reads of personal data are audited |

## The app's side

What an app writes is only its meaning (sketch, names not final):

```python
bridge = Bridge(tables, secrets={"<partner>": settings.partner_secret})
app.include_router(bridge.routes(prefix="/partners"))  # POST /partners/<partner>/webhooks
runtime.add_worker(
    bridge.worker(
        {
            ("<partner>", "payment.succeeded"): payments.on_paid,
        }
    )
)

# hand out a ref: an expectation completed by an outside event
wait, token = expect.expect(conn, tables, kind="payment", key=str(registration_id), ttl=deadline)
link = f"https://partner.example/pay?ref={token}"
```

## First scope

1. Incoming events: verify, store once, hand to the app's handler, retry, dead letter.
2. Expectations completed by an event (the ref).
3. Outside identities claimed with proof.
4. Outcome ledger v0.

## Not here (yet)

- Outgoing events and calls to outside systems (an outbox for partners): with the second bridge
  that needs them (a booking tool, through `meydan.booking`'s adapter).
- A catalogue of connections, self-service connecting, encrypted tokens: the hosting platform's
  work.
- A protocol for agents of other instances: later, and not necessarily an existing one.
