# The operator

A person on the team stepping into a conversation the agent is having: taking it over so the
agent stays quiet (**takeover**), giving it back (**handback**), writing as yourself, and who has
read what. When the *agent* hands a conversation to a person, that is a **handoff** (not built
yet).

```python
from meydan.operator.web import routes as operator_routes

app.include_router(operator_routes(auth, runtime, labels=my_labels), prefix="/operator")
```

| | what it does |
|---|---|
| `GET /operator/conversations` | observe's list, plus this reader's `unread`, any `taken_over`/`taken_by`/`taken_until`, and the app's `labels` |
| `POST /operator/conversations/{id}/read` | this reader has seen everything so far |
| `POST /operator/conversations/{id}/takeover {minutes?}` | take it over: the agent does not answer until it ends or is handed back |
| `POST /operator/conversations/{id}/handback` | hand it back to the agent |
| `POST /operator/conversations/{id}/messages {text, name?}` | write as yourself (`author="staff"`); takes it over for 30 minutes |

Reads need `ops` and `conversations:read:all`; writes also `operator:write`.

**Readers.** Read marks are per reader: a person's own actor, or, for a service key (another
application, see `auth.issue_service_key`), whom it acts for (`X-On-Behalf-Of`). A dashboard's
users each keep their own unread counts through one key.

**Takeover.** The worker checks `operator_holds` before calling the agent: a conversation taken over has its
messages are received, recorded and marked processed, with no answer. Not an error.

**Writing.** A message from the team goes to whoever wrote last, from the endpoint they wrote
to, through the same queue as every message (paced, retried, its delivery recorded). On WhatsApp
free text is refused 24 hours after their last message (`WindowClosedError`, 409): send a
template instead. A person known to another application gets an actor here through an identity
in it (`staff_actor`: scheme `staff`, their id there), so every reader sees who wrote.

**Labels.** What the app knows about the people (a role, a registered name) is its own:
`labels(actor_ids) -> {actor_id: {...}}`, shown as given. meydan has no word for them.
