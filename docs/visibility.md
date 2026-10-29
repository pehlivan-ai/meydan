# Visibility: what a signed-in person sees

Available since v0.7.0 (`meydan.visibility`).

Access answers *what may this person do* (roles and permissions, [access.md](access.md)).
Visibility answers *what does this person see* of the data meydan keeps: conversations, what was
said, addresses, what the agent did and what it cost. You choose it in one place; the read models
([observe.md](observe.md)) apply it **inside their queries**, so the console, the API and any other
caller get the same answer and a screen can't show more than allowed.

## Where it sits
```
screen (console)          shows whatever it receives
HTTP route                session → principal (who, with which scopes)
access + visibility       roles → scopes (access) · scopes → what is shown (visibility)
read model                builds the query following the visibility rules, writes audit rows
database                  data only, knows no roles
```

## Items
Each sensitive part of the data is one item with a rule: the **scope** that unlocks it, what
everyone else gets (**hidden** or **masked**), and whether each read is **audited**.

| Item | Covers | Others get |
|---|---|---|
| `operations` | overview and problems (counts, health, errors) | 403 |
| `all_conversations` | everyone's conversations (rows) | only their own |
| `message_text` | what people wrote and what the agent answered | `None` (redacted) |
| `addresses` | phone numbers and other channel addresses | masked (`+90 5•• ••• •• 67`) |
| `agent_details` | per turn: model, tokens, timings, errors | no turns |
| `costs` | money: per turn, per conversation, reports | `None` / 403 |

A person always sees their own conversations, text included.

## Presets
```python
from meydan.visibility import Rule, Visibility

Visibility.need_to_know()   # the default: each item needs its own scope
Visibility.trusted("ops")   # a small trusted team: whoever holds "ops" sees everything
```

| Item | `need_to_know()` | `trusted(scope)` |
|---|---|---|
| `operations` | `ops` | scope |
| `all_conversations` | `conversations:read:all` | scope |
| `message_text` | `messages:read`, audited | scope, audited |
| `addresses` | `addresses:read`, audited, masked otherwise | scope, audited, masked otherwise |
| `agent_details` | `ops` | scope |
| `costs` | `costs:read` | scope |

`need_to_know` is the default because a forgotten setting should show too little, not too much.
`trusted` is a deliberate choice for early teams; reads of text and addresses are still audited:
trusting people is not the same as not knowing who looked.

## Customizing
Start from a preset and change single items. A string changes only the scope (audit and masking
stay); a `Rule` replaces the whole rule:

```python
visibility = Visibility.need_to_know(
    addresses="messages:read",                       # whoever reads text sees numbers too
    agent_details="costs:read",
    message_text=Rule("messages:read", audit=True),
)
```

Scopes come from your access model: `AccessConfig(scopes={"messages:read": "can_read_messages",
...})` maps OpenFGA permissions to these names, so roles stay in `access.fga` and visibility only
says which scope unlocks which item.

## Using it
```python
from meydan.observe import query
from meydan.observe.web import routes as observe_routes

query.conversation(conn, tables, person, conversation_id, visibility=visibility)
app.include_router(observe_routes(auth, visibility), prefix="/observe")
```
