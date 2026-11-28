# Pay (`meydan.pay`)

**Status: experimental.** In this release meydan offers primitives that make it easier for meydan
app developers to take payments and connect them to meydan's core parts (identity, conversations
and turns, expectations, the outbox, bridges and connections). This page is the plan we build
against; its parts land one by one and may change as we learn. Until a part is marked available,
it is a design.

`meydan.pay` takes a payment for something an app sells, from a conversation: a card in the chat,
a page that takes the payment through the seller's own payment provider, and the messages that
follow. It is a **primitive**: an optional part (`meydan[pay]`) an app takes only if it sells.

What a payment turns into is the app's: a registration, an event ticket, an appointment
(with `meydan.booking` or the seller's own calendar), a class pack. meydan never owns capacity,
prices or rules; it asks the app through a **subject handler** and tells it what happened.

```
   the app (its own thing)                         meydan.pay
┌──────────────────────────────┐      ┌─────────────────────────────────────────────┐
│ subject: "registration:<id>" │      │ Due       who owes what, until when; a link  │
│          "ticket:<id>"       │◄────►│ Checkout  one attempt, through a Provider    │
│          "appointment:<id>"  │      │ Notices   state -> message, once, in the turn│
│ handler: payable? paid?      │      │ Surfaces  chat card, pay page, way back,     │
│          released?           │      │           events to the seller's system      │
│ capacity, waitlists, rules   │      │ Ledger    every paid due, gross / fee / net  │
└──────────────────────────────┘      └─────────────────────────────────────────────┘
```

## Concepts

- **Subject**: the app's thing a payment is for, named `kind:id`. Its **handler** says whether it
  can be paid now and for how much, and what a payment (or its loss) means.
- **Due**: one amount owed by one actor for one subject, until a deadline, with a personal link.
  The *intent*. One live due per (subject, actor).
- **Checkout**: one attempt to pay a due at a provider. The *attempt*. Its id (UUIDv7) is the
  reference the provider keeps.
- **Provider**: the adapter to a payment service. The seller brings their own; credentials are a
  meydan connection's (sealed).
- **Notice**: a rule from a state to a message ("when a due opens, send the pay card").
- **Surface**: where a person meets the due: the chat card, the pay page, the way back to the chat,
  and the seller's own systems.

## States

```
Due:       open ──► paid ──► refund_pending ──► refunded
             ├────► expired     (deadline passed, nothing paid)
             └────► cancelled   (the app or the person closed it)

Checkout:  pending ──► paid | failed | expired
           paid ──► refund_pending ──► refunded ; paid ──► withdrawn
           never opened: refused (with a reason) | error
```

A due's state follows its checkouts: it is **paid while one of its checkouts is paid** (more
precisely, while the paid amounts cover it: room for partial and split payments later, as Saleor
sums its transactions). A refund that leaves nothing paid reopens the due if its deadline allows.
Each transition happens once (`UPDATE ... WHERE state = ...`), so a result reported twice (the
browser and the poll, a retried callback) can't fulfil twice.

## The app's part: a subject handler

```python
class Registrations(pay.Subject):
    kind = "registration"

    def payable(self, conn, id) -> pay.Payable | pay.NotPayable:
        """Who pays, how much, in what currency, until when, through which connection; or why not
        (gone, closed, full, already paid). Capacity and eligibility are decided here."""

    def on_paid(self, conn, due, checkout) -> None:
        """Fulfil, in the same transaction: register, issue the ticket, confirm the booking."""

    def on_released(self, conn, due, why) -> None:
        """expired / cancelled / refunded: free what was held, if anything."""
```

Two orderings, the app's choice per kind (Saleor supports both):
- **pay to claim**: the thing is taken when paid (a place in a limited event); `on_paid` may still find it
  full and decide (an alternate list, a refund).
- **claim then pay**: the app holds it first (a booking `held`), the due confirms it;
  `on_released` frees the hold. meydan can ask the app to extend a hold while a checkout is open.

## Providers

A provider says what it needs from the person (name, email, phone, a consent) and runs the
attempt. Results come back in one of four shapes, all covered:

| shape | example | meydan |
|---|---|---|
| browser result + status fetch | an embedded form posts a signed result to the page | verify the signature, then fetch the status: the fetch decides |
| return token + retrieve | the provider posts a token to the return URL | retrieve the result server-side with it |
| signed server callback | the provider POSTs the result with an HMAC, retries until acknowledged | verify, apply once, acknowledge |
| webhooks | an event per state change | through `meydan.bridge` (signed, idempotent) |

Embedding: an iframe in the pay page, a redirect, or a hosted link. Test and live environments
are a connection's; a test key never meets a live subject (the app's rule, enforced by `payable`).
Provider errors map to reasons by status and code, never by the provider's text. Nothing a
provider sends is logged with personal data.

**Available since 0.50.0** (`meydan.pay.checkout`): checkouts, the provider protocol and the
sweep, for the browser-result shape (verify the signature, then fetch the status). The other
shapes and the `embed` mode come with the pay page API.

```python
class Provider(Protocol):
    name: str
    needs: tuple[Need, ...]  # what it needs from the person: name, email, phone (region, mobile)
    consents: tuple[str, ...]  # the consents it needs, by its own names

    def base_url(self, environment: str) -> str: ...  # test | live
    def open(self, http, base_url, token, request: OpenRequest) -> Session | Refused: ...
    def status(self, http, base_url, token, session_id, reference) -> Status: ...
    def signed(self, secret, result: Result) -> bool: ...  # the browser's result, timing-safe
```

`Checkouts(engine, tables, table, keyring, providers, subjects)` opens a session
(`open`: `pending`, or `refused` / `error` with a `Reason`; a reference the provider saw before is
retried once under a new checkout), applies a status once (`apply`: one audit row
`checkout:<status>:<provider>` and one call of the subject's handler per change), takes a browser
result as a hint (`apply_result`: signature with the connection's `hmac`, then `refresh` asks the
provider with its `token`), and sweeps (`sweep` / `run`). `requirements(provider, have)` compares
what the app knows about the person with what the provider needs. Our setup going wrong (no
keyring, a connection off, a token refused) is a `ConfigError`: loud in the logs, never shown to
the person.

## Opening a due

**Available since 0.51.0** (`meydan.pay.dues`): `Dues(tables, table, keyring, subjects)`;
`open(conn, subject, actor)` -> `(Due, token)` or `NotPayable` (the live due and the same link
when asked again, its deadline and amount brought up to what the app says); `link` (the same
token to send again: kept sealed, looked up by its hash); `resolve(conn, token)` (open and in
time, or paid until `readable_until`); `checkout_handler(checkouts_table)` for checkouts of
subject `due:<id>`; `cancel`; `expire` (past the deadline). `meydan.runtime.expect.by_token`.

`pay.open(conn, subject=, actor=, notice=True)` when the app's state says someone owes (a
registration became "awaiting payment"). It asks the subject's handler (`payable`), records the
due, and makes its link: an `expect` waitpoint of kind `pay` (token hashed, expiring at the
deadline; completed by the provider's result). Idempotent per (subject, actor): a live due is
returned, not doubled.

## Notices

**Available since 0.52.0** (`meydan.pay.notices`). A due's facts (`due.opened`, `due.paid`,
`due.expired`, ... written by `Dues` in the change's transaction, meydan.events) become messages
to the person. Patterns from notification and workflow engines (Novu: a dedupe key per trigger,
`skip` per step; Inngest: debounce with a maximum wait, sleep until a time, cancel when paid):

```python
from meydan.pay import notices as pay_notices

notices = pay_notices.Notices(
    engine,
    runtime,
    dues,
    [
        pay_notices.opened(pay_card),  # the pay card; not if paid or closed before it went
        pay_notices.paid(paid_card),  # the confirmation
        pay_notices.before_deadline(reminder_card, timedelta(hours=24)),  # once, if still open
    ],
)
runtime = Runtime(..., subscribers=notices.subscribers())  # one subscriber per notice
```

A card is `card(conn, due, reach) -> list[Outbound] | None`: `reach.window_open` says whether the
channel's reply window is open (a free-form card) or closed (an approved template; the sender
holds one still under review). meydan delivers each one **once per (due, notice)** (outbox ref
`pay:<due>:<name>`), where the person last wrote (`meydan.runtime.reach`):
- **after the agent's reply**: while a turn is about to run or running in that conversation (a
  message of theirs not answered yet: `meydan.runtime.reach.busy`), it waits, at most `max_wait_s`
  after the fact (a stuck turn never holds it back);
- otherwise at once (a due opened from a console, a sweep);
- `skip(due)` as the due stands then; `at(due)`: not before that time.

## Surfaces

- **Chat card**: a link message with a header (an image: the subject's poster or a rendered pass;
  or text), a body, a footer and one button. Needs `Link` / `Outbound.link_message` to carry
  `header` and `footer` (WhatsApp `cta_url` supports both; other channels fall back to text).
  Where a channel pays natively (WhatsApp `order_details` in some countries), a native renderer
  for the same due, later.
- **Pay page API** (`pay.api.router`): the link's token in a header, never in a path or a log;
  `GET` the due's view (subject, amount, the provider's needs against what is known, the legal
  notice to show), `POST` details (and the acknowledgement), `POST` a result (a hint), `GET` state
  (polling). Answers are `no-store`; per-client rate limits. The page's look is the app's for now;
  a web kit (states, steps, the embed, outcomes) is a later step.
- **The way back**: every outcome offers the return to the conversation (the channel's deep link)
  and, when paid, the seller's next place (their app, by the person's device).
- **The seller's systems**: due events (`due.opened`, `due.paid`, `due.expired`, `due.refunded`)
  out through `meydan.bridge`, signed and retried.

## Legal

Texts shown on the page are versioned and append-only; a person's acknowledgement (or consent,
where the law asks for one) is recorded per due with the text's version and hash, the time and the
address it came from. Which text applies is the app's (by provider and purpose).

## Ledger

Every paid due becomes an outcome record: subject, actor, provider, gross, fee, net, evidence (the
provider's reference). A platform built on meydan can measure what its agents sold, and take a
fee later: on the provider's side where it splits payments (Stripe Connect application fees), or
invoiced from the ledger.

## Sweep

Open checkouts are asked for their status until a while after they expire; paid ones are watched
until the subject's date (late refunds); dues past their deadline expire and call `on_released`;
time-based notices (`Abandoned`, `Before`) are due here. The browser is never needed for a
payment to count.

## Who pays, and paid elsewhere
A due is someone's (`actor_id`: their registration, their ticket). Who pays it may be someone
else: `payer_id` (a friend who buys the ticket, the one who invites, an organizer); `Due.payer` is
them when set, else whose it is. Notices go to the payer by default (`Notice(to="payer")`), or to
whose it is (`to="owner"`), e.g. "your ticket is paid by Ayşe".

`settle(due_id, by=, note=, who=)`: paid outside our checkouts (an organization covers it, cash, a
bank transfer). From open or expired, once; state `settled`, with who paid it (`settled_by`, an
actor: a person or an organization, e.g. an app's partner, who becomes its payer too) and,
optionally, why (`settled_note`); `who` is audited as the one who
recorded it. The app fulfils it in its subject's optional `on_settled(conn, due)` (without one,
settling that kind is refused). The link then opens as paid until `readable_until`. A checkout
paid anyway still counts (`settled` -> `paid`, `on_paid` runs): the app decides whether to refund.

Seams, not built: taking a due over ("I'll pay it": set `payer_id`, the next card goes to them),
and one checkout for several dues (a basket: a group's tickets), where the provider allows it
(some take one participant per session).

## Tables

`define_tables(metadata, prefix="pay_")`: `dues`, `checkouts`, `acknowledgements`. Provider
credentials stay in `meydan_connections`; links in `meydan_expectations`; messages in the outbox;
the ledger in the outcome ledger once it lands.

## What it does not do (yet)

Capacity, prices and eligibility (the app's); split payments between several payers and partial
payments (the states allow them, the code doesn't); recurring dues (subscriptions); invoices;
refunds requested by us (the provider decides, we follow its status); payment inside the chat
(native order messages exist only in some countries).

## Research behind it

Stripe Checkout fulfilment (idempotent, two triggers, fetch the session), Medusa payment
collections and sessions, Saleor's Transactions API (amounts summed over transactions; pay-first
or order-first), Hyperswitch (payment intent and attempt), commercetools and Stripe on holding
limited inventory during checkout, Turkish card providers' result styles (return token + retrieve;
signed callback with retries), WhatsApp's native order payments (Brazil, India, Singapore), and
chat-commerce practice (payment links, abandoned-checkout reminders, 24-hour reminders, tickets
with a QR after payment).
