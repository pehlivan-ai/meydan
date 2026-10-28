# meydan architecture

How meydan intends to coordinate people through agents, and why. Grounded in the research
summarized at the end; claims about what's built are in the README.

## Concepts

- **Person**: someone reached through a channel (a phone number on WhatsApp). Identity comes
  from the channel, never from model output.
- **Personal agent**: one per person/conversation. A *delegate*, not a negotiator: it talks with
  the person, elicits preferences, asks for consent, and relays proposals. It does not bargain
  with other people's agents in free text.
- **Planner**: one per instance. Owns group decisions with deterministic logic over shared state:
  matching, constraints, consensus, dropouts. The LLM extracts intent and writes messages; code
  decides and validates every commitment.
- **Plan**: anything that commits people (a pair, a 4-player match, a tournament entry), modelled
  as a state machine:
  `proposed → collecting → confirmed → at_risk → dropped → backfilled → done | no_show`.
- **Autonomy policy**: per person and per action, what the agent may do alone and what needs a
  "yes" first (e.g. "auto-accept Saturday mornings at my club", "always ask before paying").
- **Agreed ground**: the agent is proactive only on topics the person has named clearly and
  agreed on with it; there it follows the person's way of thinking without being asked.
  Outside that ground it asks. The ground is gardened, not just widened: the person and the
  agent revisit and refine it over time, as a practice.
- **Instance**: one community's deployment (data, planner, agents, channel). Later a **node**
  that can exchange typed messages with other instances.

## Runtime (available since v0.2)
Who is who (actors, identities, claims) and how messages find their conversation:
[docs/identity.md](identity.md) (since v0.3).

```
channel webhook ─► inbound queue (idempotent on message id) ─► 200 OK
                         │
        reader: "seen" ~1 s after arrival (docs/presence.md)
        worker: one turn per conversation at a time (Postgres advisory lock)
           debounce bursts ─► personal agent turn ─► tools / planner intents
                         │
        outbound queue ─► sender (typing indicator, bubbles, channel API, status receipts)
```

Every stage communicates through tables, so one stage can be replaced (or rewritten in another
language) without touching the others. Postgres is the only infrastructure.

## Channel integration: WhatsApp Cloud API ↔ meydan (available since v0.2)

### The contract Meta gives us
- Inbound messages and outbound delivery statuses both arrive as **webhooks** (HTTPS POST,
  signed with `X-Hub-Signature-256` = HMAC-SHA256 of the raw body with the app secret).
- The endpoint must answer **200 OK within 5 seconds**; otherwise Meta retries. Delivery is
  therefore **at least once**: the same event can arrive more than once, and the receiver must
  deduplicate. Cloud API retries undelivered webhooks (messages and receipts) for up to 7 days
  (only 1 hour on numbers with No Storage enabled), so a receiver that is down for a while
  still gets the backlog, all at once and unordered.
- Events can arrive **out of order**; order by the message `timestamp`, not arrival time.
- One Meta app has **one** webhook URL; each WhatsApp Business Account (WABA) must be
  **subscribed explicitly** to the app. Several endpoints need several apps.
- Sending is a plain HTTPS call (`POST /{phone-number-id}/messages`) that returns a message id
  (`wamid`); its fate (`sent → delivered → read`, or `failed`) comes back later as webhooks.
- Per recipient there is a **pair rate limit**: bursts are allowed, then sends to that user are
  throttled (Meta's example: after a burst of 20, wait ~2 minutes); retry with backoff
  (Meta suggests 4^n seconds).
- Business-initiated messages outside the **24-hour customer service window** must be approved
  **templates**; free-form replies are only allowed inside the window.

### Patterns
```
             ┌────────────── Meta (WhatsApp Cloud API) ──────────────┐
             │  user ⇄ WhatsApp ⇄ phone number (phone_number_id)      │
             │            webhooks (at-least-once, unordered)  ▲      │
             └────────────────────────┬─────────────────────────┼─────┘
                                      ▼                        │ POST /messages
 1 Webhook receiver ── verify signature ── route ── persist ── 200 OK (< 5 s)
                                      │                         │
 2 Inbox  (Idempotent Receiver: UNIQUE wamid; raw payload kept) │
                                      │                         │
 3 Worker (Single Writer per conversation id, Postgres advisory lock)
      Aggregator: debounce a burst into one turn; Resequencer: order by timestamp
      personal agent turn ─► tools / planner (code validates every commitment)
                                      │                         │
 4 Outbox (Transactional Outbox: replies written in the same transaction as state)
                                      │                         │
 5 Sender ── per-number + per-recipient Rate Limiter ── Retry w/ jittered backoff ┘
      Circuit Breaker on repeated 5xx; Dead Letter after N attempts; typing indicator
                                      │
 6 Status correlation: delivery webhooks matched to outbox rows by wamid
      (sent/delivered/read/failed) ─► reliability signals, re-send or template fallback
```

| Stage | Pattern | Why |
|---|---|---|
| 1 Receiver | Webhook receiver, Message Router, verify-then-ack | Meta's 5 s budget: do nothing slow before 200; reject unsigned payloads |
| 2 Inbox | Idempotent Receiver / Transactional Inbox | at-least-once delivery → dedupe on `wamid`; keep raw payloads (they can't be fetched again) |
| 3 Worker | Single Writer, Aggregator, Resequencer | one turn at a time per conversation; people send bursts; events arrive unordered |
| 4 Outbox | Transactional Outbox | never send a reply for state that didn't commit, never lose a reply for state that did |
| 5 Sender | Rate Limiter, Retry with backoff + jitter, Circuit Breaker, Dead Letter Channel | pair rate limits, transient API errors, and no infinite retries |
| 6 Statuses | Correlation Identifier (`wamid`) | asynchronous delivery results; feeds reliability and the 24 h window logic |

### n:n relationships
The integration is many-to-many at several levels, so meydan never assumes "one number, one
app, one community":

```
Business portfolio 1──n WABA 1──n phone number (phone_number_id)
Meta app           n──n WABA          (a WABA can be subscribed to several apps; one app, one URL)
phone number       n──1 instance      (routing table: phone_number_id → instance)
instance           1──n phone number  (e.g. a temporary and a local number side by side)
person (actor)     1──n identity      (tel, wa-bsuid, mailto, ...; linked by claims: docs/identity.md)
person             n──n phone number  (the same person can write to several numbers)
conversation       = (our endpoint, the person) → one personal agent; its id is the key
person             1──n conversation  (one per endpoint they use; history follows the person)
person             n──n plan          (pairs, groups, waitlists; via plan participants)
plan               n──n conversation  (the planner fans proposals out to each participant's
                                       conversation and fans replies back in)
```

Consequences:
- **Routing** uses `entry[].changes[].value.metadata.phone_number_id` from each webhook, never a
  single configured number. One receiver can serve many instances (a multi-tenant gateway) or
  one instance per receiver; both are routing-table configurations.
- **Reply affinity**: a reply always goes out from the `phone_number_id` the person wrote to,
  since the 24 h window, templates and pair rate limits are all per (number, recipient).
- **Fan-out / fan-in** for groups: the planner never talks to people directly. It writes one
  proposal per participant into each participant's conversation (outbox), and each personal
  agent turns replies into typed answers the planner aggregates (Scatter-Gather).
- **Identity**: a person is keyed by `wa_id` per instance; linking the same human across
  instances (later, between peers) is explicit and consented, never inferred.

## Group dynamics (planned)

- **Consensus over chat**: the planner proposes 2-3 concrete options (time + place) as buttons;
  a rule closes the decision (quorum by a deadline, or least-misery). No open-ended negotiation.
  Which rule works best is an experiment, not an assumption.
- **Matching**: two-sided matching where the problem is two-sided (always has a stable result);
  scored greedy heuristics for any-pair and group formation (stability can't be promised there).
- **Social ties**: who played/met with whom, regular partners, invite/accept history. Negative
  preferences ("rather not with X") are used for matching and never revealed.
- **Consent**: committing another person (adding a partner) needs that person's confirmation.
- **Dropouts**: a ranked waitlist (constraint fit, reliability, fairness to newcomers) is offered
  the spot in order; escalate to an organizer near the deadline.
- **Reliability**: record confirmed-vs-attended per person from day one; the literature has no
  ready model, so each instance learns from its own data.
- **Preferences**: hard constraints separate from soft scored preferences; learn from which
  offered option people pick.

## Phases

1. One instance, in-process: runtime (worker + sender), personal agents, planner, Postgres. No
   agent-to-agent protocol.
2. More roles (organizers, venues, sponsors) as separate agents with typed, A2A-shaped
   interfaces, still in-process.
3. Expose A2A/MCP endpoints between instances when there are external peers. Consider
   privacy-preserving or P2P coordination only if parties stop trusting a single operator.

## Why this shape (evidence)

- Personal-agent deployments (Electric Elves, 2000-2002; CALO/PTIME, 2011) found adjustable
  autonomy and privacy mattered more than agent-to-agent mechanics. Scerri, Pynadath & Tambe,
  arXiv:1106.4573; Berry et al., ACM TIST 2011.
- Distributed constraint optimization for meeting scheduling is motivated by privacy without a
  trusted party, mostly evaluated in simulation with honest agents (Maheswaran et al., AAMAS
  2004). No evidence found that it beats a trusted coordinator when one operator holds the data.
- LLM agents are weak at multi-user scheduling and negotiation (Muses-Bench, arXiv:2604.08567;
  NegotiationArena, ICML 2024; arXiv:2309.17234), with model-dependent exceptions
  (arXiv:2602.05302).
- Centralized orchestration contains error amplification best; multi-agent setups degrade on
  sequential tasks (Kim et al., arXiv:2512.08296). Most multi-agent failures are design and
  inter-agent misalignment (Cemri et al., arXiv:2503.13657).
- Group recommendation has no universally best aggregation rule (social-choice limits); choose
  per context and test.

These are summaries of abstracts and secondary sources, not full replications; treat them as
design guidance.
