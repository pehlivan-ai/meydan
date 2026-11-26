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

```python
class Provider(Protocol):
    name: str
    embed: Literal["iframe", "redirect", "link"]
    def needs(self) -> list[Need]: ...
    def open(self, http, env, creds, checkout, person, consents, return_url, embed_origin) -> Opened: ...
    def status(self, http, env, creds, checkout) -> Status: ...
    def verify(self, creds, result) -> Result | None: ...          # browser or callback result
```

## Opening a due

`pay.open(conn, subject=, actor=, notice=True)` when the app's state says someone owes (a
registration became "awaiting payment"). It asks the subject's handler (`payable`), records the
due, and makes its link: an `expect` waitpoint of kind `pay` (token hashed, expiring at the
deadline; completed by the provider's result). Idempotent per (subject, actor): a live due is
returned, not doubled.

## Notices

Rules in code, per subject kind:

```python
notices = [
    pay.Opened(card=pay_card),                          # the pay card
    pay.Paid(card=paid_card),                           # the confirmation
    pay.Abandoned(after=timedelta(hours=2)),            # a checkout opened, nothing paid
    pay.Before("deadline", timedelta(hours=24)),        # one reminder
]
```

meydan delivers each one **once per (due, rule)** (outbox ref `pay:<due>:<rule>`), audited:
- inside the channel's window as an interactive card; outside it as the approved template the rule
  names; with neither, recorded as not delivered, with the reason, for the team to see;
- **in the turn**: if the due opened while the actor's conversation has a turn running (the agent's
  tool caused it), the card joins that turn's reply after the model's bubbles; if the model already
  attached the same card, not twice. Otherwise it goes on its own (the team did it in ops, a sweep
  did it). The rule decides that a card goes; the turn decides only where.

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
