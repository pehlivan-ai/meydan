# Party (`meydan.party`)

**Status: experimental.** The first primitive for things people do *together*. It may change as
the first apps use it.

A **party** is the app's shared thing with places for people: a team of two for a tournament, a
match of four, a table of six, friends going to a concert. A **seat** is one place in it: who sits
there (or who was asked to), in which role, and its state. What the party is *for* stays the app's:
its `kind` (`"app.team"`, the app's name first so kinds of different apps never clash) and its
`subject` (`"tournament:<id>"`, `"booking:<id>"`, or none).

```
   the app                                       meydan.party
┌────────────────────────────────┐     ┌───────────────────────────────────────────┐
│ kind: "app.team"  capacity 2   │     │ Party   kind, subject, capacity, state     │
│ subject: "tournament:<id>"     │◄───►│ Seat    role, state, who, how it was taken │
│ handler: on_accepted (register,│     │ Doors   address (ask) · code · claim       │
│          open a due) ·         │     │ Facts   seat.invited/accepted/declined/... │
│          on_released           │     └───────────────────────────────────────────┘
│ words, templates, rules        │
└────────────────────────────────┘
```

## Kinds: capacity and the rest are data

```python
parties = Parties(
    runtime_tables,
    party_tables,
    {
        "app.team": Kind("app.team", Capacity.exactly(2), TeamHandler()),
        "app.match": Kind("app.match", Capacity.exactly(4)),
        "app.concert": Kind("app.concert", Capacity(min=1, max=None)),  # no ceiling
        "app.class": Kind("app.class", Capacity(min=4, max=8)),  # happens once 4 accept
    },
)
```

- **Capacity**: at least `min` accepted seats to be `confirmed`; at most `max` invited + accepted
  (`None`: no ceiling). Invitations past their time no longer count.
- **invite_ttl**: how long an invitation runs (default three days).
- **circle** (`auto | ask | none`): a seam, documented and not built. A person's *circle* is the
  people they know and trust; accepting a seat may add the people of a party to each other's
  circles. Each kind says whether that happens by itself, is asked, or never happens.

The **handler** runs in the change's transaction: `on_accepted(conn, party, seat, door)` (open a
registration, a due) and `on_released(conn, party, seat)` (declined, expired, left). Each returns
the messages to say back when the change came from a pressed button.

## States

```
Party: forming ──► confirmed (accepted seats reach min) ; cancelled
Seat:  invited ──► accepted ──► left
               ├─► declined
               └─► expired      (nobody took it in time)
```

Each transition happens once (`UPDATE ... WHERE state = ...`). One person holds at most one
accepted seat among the parties of one kind and subject (`already_in`); nobody takes their own
invitation (`own`).

## Three doors to a seat

| Door | How | Proof |
|---|---|---|
| **address** | `ask` (a contact we start, usually a template) or `ask_in` (in a conversation they have with us, while the reply window is open): buttons on a meydan expectation (kind `party`), answered through `handler()` | meydan expect's checks: the token, the asking message, the address's owner, in time, once |
| **code** | `invite` gives each seat a short code (`#K7Q2MX`, no look-alike characters); the inviter forwards it themselves (in a link with a prefilled message, say); `find_code(text)` finds it in a message, `redeem` takes the seat | holding the code: an invitation, not a secret. Taking the seat is still the person's own step, and the app decides what it costs |
| **claim** | the person says who invited them; the app finds that inviter's open seats (`invited_by`) and asks them to confirm, then `accept(door=Door.CLAIM)` | the weakest: the facts carry the door, so the app can tell the inviter who took it |

A seat taken through one door closes the others (a live ask is cancelled). Asking someone at an
address settles nobody on a guess (`plan_contact`); when the channel later names two of their
addresses together (a phone and a username), meydan's identity joins them.

## Facts

In the change's transaction (meydan.events), `subject_id` the seat's id (the party's for
`party.confirmed`), data `{party_id, kind, subject, door, role}`: `seat.invited`, `seat.accepted`,
`seat.declined`, `seat.expired`, `seat.left`, `party.confirmed`. Apps subscribe to send the
invitation (`seat.invited` -> `ask` or `ask_in`) and to tell the inviter.

## Tables

`define_tables(metadata, prefix="meydan_", name="parties")` puts `parties` and `parties_seats` on
the app's metadata; the app's migrations create them (as with `meydan.pay`).

## Seams (not built)

- **Gathering**: a party that has a time or a topic ("Saturday 18:00, court 3").
- **Plan**: the way to a gathering (proposals, agreement, filling a seat that frees up).
- **Circle**: a person's own known and trusted people, grown from the parties they were in, the
  ground a personal agent plans and coordinates on, across apps, with the person's permission.
- **Who may invite**: today any seated person, through the app's own rules; invitation rights
  (an invite-only community) would be a kind's setting.
