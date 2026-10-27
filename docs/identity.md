# Identity and conversations

Who is who across channels, and which conversation a message belongs to. Available since v0.3
(`meydan.runtime.identity`, tables in `meydan.runtime.tables`).

## The problem
People, agents and organizations talk to each other on several channels (WhatsApp and email
built in; anything else through a connector). The same person shows up under several addresses:
a phone number, a WhatsApp business-scoped user id (BSUID) when they use a username and don't
share their number, an email address. Addresses appear, disappear and rotate. A conversation
keyed by one address (v0.2 used `channel:our_number:their_number`) breaks every time that
happens, and a model that merges people on a guess can't be undone.

## Primitives

| Primitive | What it is |
|---|---|
| **Actor** | Anything that acts or is addressed: `person`, `agent` or `org`. |
| **Identity** | An immutable address on a channel: `(scheme, scope, value)`, e.g. `tel:+15550000000`, `wa-bsuid:<portfolio>:US.123…`, `mailto:…`, `wa-pnid:<phone_number_id>` (our side). Names and usernames are attributes, never keys. |
| **Claim** | Append-only evidence that an identity belongs (`active`) or may belong (`proposed`) to an actor, with a `basis` and references as `evidence`. Closed by `valid_to`, never edited. The only place who-is-who is decided. |
| **Conversation** + **Participant** | An exchange between identities on one of our endpoints: `direct`, `thread` (email) or `group`. Its id is the conversation key: the unit of one-turn-at-a-time, reply routing and history. |
| **Turn** | One participant's ordered burst. Inbound: the worker aggregates a burst (debounce). Outbound: an agent's reply is one turn of one or more messages (outbox `turn_id`, `seq`). |
| **Connector** (code) | `parse` a channel's payload into messages with addresses (plus addresses the channel asserts are the same sender), `send` to an address. |

**Rule that keeps it reversible:** everything raw (inbox, outbox, participants) points to
identities; "actor" is a view through claims. Re-linking an identity closes one claim and opens
another, and no message is rewritten.

```
actor ──< claim >── identity ──< participant >── conversation
                       ▲                              ▲
                       └── sender / recipient ── inbox / outbox (turns)
```

## Settlement policy (defaults)

| Basis | When | Default |
|---|---|---|
| `endpoint` | our own number/mailbox seen for the first time | owned by a new `agent` actor (apps may rename or re-claim it) |
| `first_contact` | an unknown address writes | a new `person` actor owns it |
| `channel_cooccurrence` | the channel asserts two addresses in one signed message (WhatsApp: phone + BSUID) | unowned addresses join the owner automatically |
| (conflict) | co-occurring addresses already belong to two actors | a `proposed` claim, once; **never merged automatically** |
| `proof_of_control`, `person_confirmed`, `self_asserted`, `org_attested`, `heuristic` | planned (challenges, confirmations) | link only on proof; self-asserted phones/emails are never linked on their own; names never link |

Settlement takes an advisory lock per address, so concurrent webhooks for a new person can't
create two actors.

## Routing
A message goes to the direct conversation between our endpoint and the sender's **actor**, found
through any identity the actor owns. A person who hides their number (BSUID only) or later shares
it stays in the same conversation; the new identity is added as a participant. Replies go to the
address that wrote last.

`transcript(conn, tables, conversation_id)` returns what was said, both directions, ordered on
the database clock (a sender's device clock must not interleave with ours).

## Channels

**WhatsApp** (`meydan.channels.whatsapp`): `from` → `tel` (E.164), `from_user_id` → `wa-bsuid`
scoped by `parse_messages(payload, portfolio=...)` ("" for a single business portfolio), both in
one message → `also` (co-occurrence); `contacts[].profile.username` → identity attrs;
`metadata.phone_number_id` → `wa-pnid`. Sending uses `to` for a phone number and `recipient` for
a BSUID.

**Email** (planned): sender address → `mailto`; `References`/`In-Reply-To` root → conversation
`thread_key`.

**Custom connector:** emit `Inbound(to, sender, also, ...)` with your own schemes and implement
`send`. Nothing else in meydan changes.

## Not decided here (the app's)
Domain roles (players, members, customers) are the app's tables referencing `actor_id`. Which
extra data to collect (e.g. a phone number at registration) and when to verify it, too.

## Roadmap
1. v0.3: actors, identities, claims, conversations, participants, turns; WhatsApp BSUIDs.
2. Challenges (proof of control), confirmations, unmerge by claim batches, audit view.
3. Email connector (threads); the connector contract frozen after two real implementations.
4. Relations between actors: `acts_for` (delegation), `member_of`; `on_behalf_of` on messages.
5. Groups and agent-to-agent peers.
