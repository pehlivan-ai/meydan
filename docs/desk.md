# The desk

People on the team working the conversations next to the agent: who has read what, taking a
conversation over so the agent stays quiet, and writing to someone as yourself.

```python
from meydan.desk.web import routes as desk_routes

app.include_router(desk_routes(auth, runtime, labels=my_labels), prefix="/desk")
```

| | what it does |
|---|---|
| `GET /desk/conversations` | observe's list, plus this reader's `unread`, any `held`/`held_by`/`held_until`, and the app's `labels` |
| `POST /desk/conversations/{id}/read` | this reader has seen everything so far |
| `POST /desk/conversations/{id}/hold {minutes?}` | take it over: the agent does not answer until it ends or is handed back |
| `POST /desk/conversations/{id}/release` | hand it back to the agent |
| `POST /desk/conversations/{id}/messages {text, name?}` | write as yourself (`author="staff"`); takes it over for 30 minutes |

Reads need `ops` and `conversations:read:all`; writes also `desk:write`.

**Readers.** Read marks are per reader: a person's own actor, or, for a service key (another
application, see `auth.issue_service_key`), whom it acts for (`X-On-Behalf-Of`). A dashboard's
users each keep their own unread counts through one key.

**Holding.** The worker checks `desk_holds` before calling the agent: a held conversation's
messages are received, recorded and marked processed, with no answer. Not an error.

**Writing.** A message from the team goes to whoever wrote last, from the endpoint they wrote
to, through the same queue as every message (paced, retried, its delivery recorded). On WhatsApp
free text is refused 24 hours after their last message (`WindowClosedError`, 409): send a
template instead. A person known to another application gets an actor here through an identity
in it (`staff_actor`: scheme `staff`, their id there), so every reader sees who wrote.

**Labels.** What the app knows about the people (a role, a registered name) is its own:
`labels(actor_ids) -> {actor_id: {...}}`, shown as given. meydan has no word for them.
