# Routing: a handler per kind of sender

Available in `meydan.runtime` (`route_by`).

An app often talks to more than one kind of person: the team and the clients, members and
strangers, or two numbers with two jobs. Each kind gets its own handler, and code decides which
one a turn goes to.

```python
from meydan.runtime import Runtime, route_by


def role_of(turn: Turn) -> str | None:
    with engine.connect() as conn:
        return roles.of(conn, turn.actor_id)  # your lookup; None: we don't know them


handler = route_by(role_of, {"staff": staff, "member": member, "guest": guest}, default="guest")
runtime = Runtime(engine, runtime_tables, channel, handler)
```

`resolve(turn)` returns a key. It may be plain or async, and may use anything on the turn: who
wrote (`turn.actor_id`), the number that was written to (`turn.to`), the channel. meydan does not
know what the keys mean.

## What stands behind a key is yours

A handler is `Turn -> list[Outbound]`. Behind a key there can be one agent, several agents, fixed
text, or another `route_by`:

```python
by_number = route_by(lambda t: t.to, {CLIENTS: clients, TEAM: team}, default=CLIENTS)
```

## What the lookup found travels with the turn

A lookup usually finds more than the key: a name, a profile, a credential for the systems the
tools call. Return `(key, found)` and the handler gets the turn with `turn.resolved = found`, so
nobody is looked up twice:

```python
def who(turn: Turn) -> tuple[str | None, Person | None]:
    person = people.find(turn.actor_id)
    return (person.role, person) if person else (None, None)
```

## Three rules

| Rule | Why |
|---|---|
| The key is resolved again on every turn | a role that was taken away stops working with the next message, not when a cache expires |
| No key, or a key nothing is registered for, goes to `default` (the second is logged) | you say who answers a stranger; give that handler the least it needs |
| If `resolve` fails, the turn fails and is tried again like any other turn | someone you could not look up is not answered as if they were a stranger |

## Why the key is not chosen by a model

Frameworks route by what a message is about: a model reads it and picks a specialist. That fits
topics, and a model can be wrong about them. Who someone is, is not a matter of reading: a
stranger who writes "I am the manager" is still a stranger. Decide the key from what your data
says about the sender; inside one handler, routing by topic with a model is yours to add.

## What it is not

Authorization. Routing decides who answers and which tools that agent has. Whether this person
may do this thing is checked again where it is done: in the tool, with the actor the channel
settled ([`meydan.tools`](../src/meydan/tools.py)), and in whatever system the tool calls.
