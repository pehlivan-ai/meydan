# Bridges: outside systems talk to an instance

Status: incoming events and expectations completed by an event are **available** (v0.40.0);
connections with sealed credentials and public-key signatures (`v2a`) since v0.41.0; credentials
for calling them and checking what they sign (`token`, `hmac`) since v0.43.0. The rest of this
page is the design they belong to.

The README's island: "later, bridges connect it to other islands". A bridge is that link, and the
other island may be another meydan instance or a system that is not meydan at all: a
stakeholder's own software, a payment provider, a booking tool the business already uses.

## Why

A meydan app connects people (A with B) and records when a connection reaches an outcome: a
booking confirmed, an order paid, a delivery made. The systems around those people already
exist and the app should not ask anyone to replace them ("them or us"). It works next to them:
the outside system stays the record of what it owns (payments, a calendar, an order book), and
the meydan app is where people talk and decide. A bridge carries the facts between the two.

Payment shows the shape. Whoever collects the money (a stakeholder's system, the app's own
payment provider, or a marketplace that splits it), the app needs one fact: "what was owed under
this reference is paid". Who collected it stays behind the bridge, so changing who collects
changes the bridge, not the app.

## Words

Three separate things. An outside system may have any of them, and an app that needs none of
them takes none of them (`meydan.bridge` is optional, like every primitive).

| Word | Question | Examples |
|---|---|---|
| **Connection** | Is there a technical link, and which way? | a payment provider tells us (in), a weather API we call (out), a messaging platform (both) |
| **Credential** | Who proves what? | their shared secret (`whsec_`, Standard Webhooks `v1`), their public key (`whpk_`, `v2a`), the API token they gave us (`token`), the secret they sign their results with (`hmac`), nothing (open data) |
| **Principal** | Are there people or agents on their side who sign in and act? | a stakeholder's staff on their own page; another instance's agents. Optional |

A connection holds credentials for either direction. **In**: what their requests to us are
checked with (`whsec`, `whpk`); only these ever verify a request. **Out**: what we need when we
call them or read what they send back: a `token` we present to their API (as a bearer token, an
API key header: the app's call), and an `hmac` secret they sign their results with (which
algorithm and what exactly is signed is their format, so checking it is the app's code). Both
are usually **theirs**. A connection may hold only one direction: one we only call has no
`whsec`/`whpk`, so a request in its name is refused as unknown.

What the relationship behind a connection is called (a business word) is the app's choice;
meydan names only the technical link. When people on the other side sign in, they are one of
the app's **stakeholders** (README), with roles like anyone else.

## Concepts

- **Connection**: one outside system an instance is linked to, by the app's name for it:
  whether it is on, its credentials (sealed, never in code). It acts only within its own scope.
- **Credential**: how a connection proves itself, or what we prove ourselves with to it. Kinds:
  a shared secret (`whsec_`, `v1`), or their public key (`whpk_`, `v2a`: we hold nothing that
  could forge their events); for calls, a `token` to their API and an `hmac` secret they sign
  their results with. Origin: **ours** (we generated it and handed it over) or **theirs** (they
  generated it and gave it to us). Each has a validity window, so a new one and the old one both
  work while they rotate.
- **Event**: one fact the outside system tells us (`payment.succeeded`, `booking.cancelled`),
  with an id of its own, a type, a time and data. Events arrive at least once and in any order.
- **Reference (ref)**: an unguessable token the app hands out (in a link, a message) so that a
  later event can be tied to what it is about without sharing personal data. A ref is the token
  of an **expectation** (`docs/expect.md`): something the app waits for, completed once from
  outside, with an expiry.
- **Operation**: a typed action of the app (`book`, `hold`, `settle`) on behalf of an actor.
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
                                 the other side's
                                 servers, another
                                 instance)
```

## Guarantees (whatever the transport)

| Guarantee | How |
|---|---|
| Only the connection can speak for itself | Signed requests, [Standard Webhooks](https://www.standardwebhooks.com/) (`webhook-id`, `webhook-timestamp`, `webhook-signature`): `v1` with a shared secret, or `v2a` with their key pair. A channel's own format (Meta's `X-Hub-Signature-256`) only where that channel dictates it |
| No replays | The timestamp must be within 5 minutes, either way |
| Once | Events are stored first, unique on (connection, event id); a repeat is acknowledged and ignored |
| Fast answer | Verify, store, 2xx. Handling happens in a worker, as with messages |
| Nothing lost | Raw payload kept; a failing handler is retried with backoff, then dead-lettered with its reason |
| Secrets at rest | Sealed with AES-256-GCM (meydan.vault), bound to their row; lists show a fingerprint |
| Never on a guess | An outside user id becomes an identity only with proof (a claim with basis `proof`, e.g. the event carries a ref we issued to that person), never by matching a name or an email |
| Recorded | Every event, its handling and every outcome can be read by the team (observe), and reads of personal data are audited |

## The app's side

What an app writes is only its meaning:

```python
from meydan.bridge import Bridge, PermanentError
from meydan.bridge.web import routes as bridge_routes
from meydan.runtime import expect
from meydan.vault import Keyring

bridge = Bridge(
    engine,
    tables,
    keyring=Keyring.from_env(),  # credentials from the table
    handlers={("acme", "payment.succeeded"): on_paid},
)
app.include_router(bridge_routes(bridge), prefix="/connections")  # POST /connections/acme/webhooks
asyncio.create_task(bridge.run())  # in the app's lifespan

# hand out a reference: an expectation completed by an outside event
exp, token = expect.expect(
    conn,
    tables,
    kind="payment",
    channel="bridge",
    options=["paid"],
    ttl=timedelta(days=3),
    key=str(order_id),
)
link = f"https://pay.example/checkout?ref={token}"


def on_paid(conn, event):  # in the event's own transaction
    try:
        exp = expect.complete(
            conn,
            tables,
            event.data["ref"],
            kind="payment",
            answer="paid",
            evidence={"event": event.event_id},
        )
    except expect.NotCompletedError as e:
        raise PermanentError(e.reason) from None  # retrying won't help: dead, with the reason
    ...  # what "paid" means to the app
```

The URL prefix is the app's choice. `secrets={"acme": "whsec_..."}` (from config) works too, and
wins over the table for a name it has: the simplest start, with no keyring.

## Events (available since 0.40)

| Part | Does |
|---|---|
| `Bridge(engine, tables, handlers=, secrets=, keyring=, tolerance=, attempts=)` | Credentials from `secrets` (connection → a secret, or several) and, with a `keyring`, from the connections table. A connection with neither, or switched off, is unknown (404). `handlers`: (connection, type) → `handler(conn, event)` |
| `Bridge.receive(conn, connection, headers, body)` | Verify (`v1`, `v2a`; versions without a credential are ignored), parse (a JSON object with a `type`), store once per (connection, `webhook-id`). Raises `RejectedError(status)`: 404 unknown connection, 400 malformed, 401 bad signature or a timestamp more than 5 minutes off |
| `Bridge.run()` / `drain()` | One event per transaction, locked (`SKIP LOCKED`); the handler runs in a savepoint. Success: `done`. `PermanentError`: `dead` at once. Another error: again after 30 s, 1 min, 2 min, ... (at most 6 h), then `dead` after `attempts` (8) |
| `Bridge.retry(conn, id)` | Put a `dead` or `ignored` event back in line |
| `meydan.bridge.web.routes(bridge)` | `POST /{connection}/webhooks`: 200 `{"received", "duplicate"}` once stored (a repeat too), 413 over 256 KB |
| `new_secret()`, `new_keypair()`, `sign()`, `sign_v2a()`, `headers()`, `verify()` | For senders written in Python, tests and the other side's first try |
| `python -m meydan.bridge send URL --type T --data JSON` | Be the other side: sign (`$MEYDAN_BRIDGE_SECRET`, or `$MEYDAN_BRIDGE_SIGNING_KEY` + `--endpoint-id`) and send one event; `secret`, `keypair` make credentials |

Table `meydan_bridge_events`: connection, event_id (unique together), type, payload (the whole
body), sent_at (its signed time), received_at, status (`pending`, `done`, `ignored`, `dead`),
attempts, next_at, handled_at, last_error.

A sender following Standard Webhooks retries on anything but 2xx and stops sending to an
endpoint that answers 410; the routes never answer 410. Its payloads should stay small (the spec
suggests under 20 KB): an event says what happened and the reference, not a whole record.

## Connections and credentials (available since 0.41, extra `bridge`)

`meydan.bridge.connections`, in the caller's transaction (recording who did it is the caller's):

| Function | Does |
|---|---|
| `ensure(conn, t, name, endpoint_id=, note=)` | Create the connection, or set its `v2a` endpoint id / note |
| `set_status(conn, t, name, on)` | Switch it on or off (off: its requests get 404) |
| `generate_secret(conn, t, keyring, name, by=)` | A shared secret of **ours**, returned to hand over |
| `add_secret(conn, t, keyring, name, secret, by=)` | A shared secret of **theirs** |
| `add_public_key(conn, t, keyring, name, "whpk_...", endpoint_id=, by=)` | Their public key (`v2a`); needs the endpoint id they gave our endpoint |
| `add_credential(conn, t, keyring, name, kind, value, origin="theirs", by=, valid_until=)` | Any kind; for calls, a `token` for their API or the `hmac` secret they sign with (1 to 4096 characters, nothing blank around it; `whsec`/`whpk` checked as above) |
| `value(conn, t, keyring, name, kind)` | The newest live one of a kind, opened (the token to send, the hmac secret to check with); None if none, or the connection is unknown or off |
| `rotate(conn, t, keyring, name, overlap=7 days, by=)` | A new secret of ours; the live ones end after `overlap` |
| `retire(conn, t, credential_id, after=)` | End one now or later |
| `credentials(conn, t, keyring, name)` | What its requests may prove themselves with now (what `Bridge` uses): live `whsec` and `whpk` only, never a `token` or an `hmac` |
| `listing(conn, t, name)` / `reveal(conn, t, keyring, id)` | Without values (kind, origin, fingerprint, validity) / one value, for whoever may see it (audit it) |
| `reseal(conn, t, keyring)` | Seal again with the keyring's current key |

Tables: `meydan_connections` (name, status, endpoint_id, note) and
`meydan_connection_credentials` (connection, kind `whsec`/`whpk`/`token`/`hmac`, origin
`ours`/`theirs`, sealed, fingerprint, valid_from, valid_until, created_by).

Calling them is the app's own code for now (its HTTP client), with what `value` gives:

```python
with engine.connect() as conn:
    token = connections.value(conn, tables, keyring, "acme", "token")
    signing = connections.value(conn, tables, keyring, "acme", "hmac")
if token is None:
    raise RuntimeError("acme: no live token (none given, retired, or the connection is off)")
answer = httpx.get(
    "https://api.acme.example/v1/orders/42", headers={"Authorization": f"Bearer {token}"}
)
# check `answer`'s signature with `signing` the way their documentation says
```

A new token: add it (`add_credential`; the newest live one is used at once), then `retire` the
old one, now or after a while. Switching the connection off stops both ways: its requests get
404 and `value` gives nothing.

### Managing them over HTTP (available since 0.42, extras `api`, `bridge`)

`meydan.bridge.manage.routes(bridge, auth, may=, audit=, names=, reveal=False)`: what a
connection's own page, or the team's, needs. The app decides everything:

- `may(principal, connection, action)`: `read` (see it, its credentials without values, its
  events and refused requests) or `manage` (create, accept, rotate, retire credentials; add a
  public key, a token or an hmac secret of theirs; retry an event; switch it on/off). Nothing is allowed by default, admins included;
  reading does not imply managing. May be async (an OpenFGA check). Unknown or not allowed: 404,
  so names stay private.
- `audit(conn, principal, action, connection)`: called in the change's own transaction.
- `reveal`: showing a stored secret again is **off** by default. A secret is shown once, when it
  is made; with `reveal=False` nobody can read it back, so an app can keep secrets away from its
  own admins. `reveal=True` adds an audited "show again".

| Route | Does |
|---|---|
| `GET ""`, `GET /{name}` | the connections this person may read; one |
| `GET /{name}/events`, `GET /{name}/rejections` | its latest events (with payload and outcome); its latest **refused** requests and why (bad signature, old timestamp, no type; no bodies kept, 30 days) |
| `POST /{name}/secrets` | `{}`: a secret of ours, returned once; `{"secret": "whsec_..."}`: theirs; `{"kind": "token" \| "hmac", "secret": "..."}`: their token or hmac secret (never generated here; not returned; audited `token:theirs` / `hmac:theirs`) |
| `POST /{name}/public-keys`, `POST /{name}/rotate` | their `whpk_` key (+ endpoint id); a new secret of ours, old ones end after `overlap_days` |
| `POST /{name}/credentials/{id}/retire`, `.../reveal` | end one (now or later); show one again (only with `reveal=True`) |
| `POST /{name}/events/{id}/retry`, `POST /{name}/status` | put a dead/ignored event back; on/off |

Refused requests of a known connection are kept in `meydan_bridge_rejections` (connection,
status, reason, webhook id, time): what the other side most needs while setting up.

**Who creates the secret?** Either side; the code is the same. If their system already makes
webhook secrets, take theirs (`add_secret`). If not, make one (`generate_secret`) and hand it over
on a safe channel, never in a chat. Safest: they keep a key pair and give us only the public key
(`v2a`), so nothing we hold can forge their events.

**The keyring** (`meydan.vault.Keyring`): `MEYDAN_KEYRING="k1:<base64 32 bytes>"`, new key with
`Keyring.new_key()`. To change it: put the new key first (`k2:...,k1:...`), `reseal`, then drop
the old one. Keep it with the app's other secrets, not in the database it protects.

## First scope

1. Incoming events: verify, store once, hand to the app's handler, retry, dead letter. **Done.**
2. Expectations completed by an event (the reference). **Done** (`expect.complete`).
3. Connections with sealed credentials, ours or theirs, `v2a`. **Done** (0.41); credentials for
   calling them (`token`, `hmac`), 0.43.
4. Outside identities claimed with proof.
5. Outcome ledger v0.

## Not here (yet)

- Outgoing events and calls to outside systems (an outbox for them: retries, a record of each
  call): with the second bridge that needs them (a booking tool, through `meydan.booking`'s
  adapter). Until then a call is the app's code, with the connection's `token` (0.43).
- Typed events: a model per event type, checked on arrival, its schema published as docs.
- Self-service connecting across apps: the hosting platform's work.
- A protocol for agents of other instances: later, and not necessarily an existing one.
