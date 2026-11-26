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
| `GET /operator/conversations/{id}` | one conversation's row (who, unread, takeover) |
| `GET /operator/summary` | counts only (`taken_over`), nothing read or audited |
| `POST /operator/conversations/{id}/read` | this reader has seen everything so far; the person writing sees "seen" for what was unread (`conversation.read`, the only "seen" during a takeover) |
| `POST /operator/conversations/{id}/takeover {minutes?}` | take it over: the agent does not answer until it ends or is handed back |
| `POST /operator/conversations/{id}/handback` | hand it back to the agent |
| `POST /operator/conversations/{id}/messages {text, name?}` | write as yourself (`author="staff"`); takes it over for 30 minutes |

Reads need `ops` and `conversations:read:all`; writes also `operator:write`.

**Readers.** Read marks are per reader: a person's own actor, or, for a service key (another
application, see `auth.issue_service_key`), whom it acts for (`X-On-Behalf-Of`). A dashboard's
users each keep their own unread counts through one key.

**Takeover.** While a conversation is taken over the agent does not answer it: the worker skips it, checks again in the reply's own transaction (a takeover that lands while the agent thinks still stops the reply), and the agent's unsent replies are cancelled. Its messages wait: what a person answers is marked answered, and the agent answers what is still unanswered once it is handed back or the takeover ends. Someone else's takeover is not overwritten unless `replace` (409). Writing never shortens a takeover. Takeover and handback are audited. Before: a conversation taken over had its
messages are received, recorded and marked processed, with no answer. Not an error.

**Writing.** A message from the team goes to whoever wrote last, from the endpoint they wrote
to, through the same queue as every message (paced, retried, its delivery recorded). On WhatsApp
free text is refused 24 hours after their last message (`WindowClosedError`, 409): send a
template instead. A person known to another application gets an actor here through an identity
in it (`staff_actor`: scheme `staff`, their id there), so every reader sees who wrote.

**Labels.** What the app knows about the people (a role, a registered name) is its own:
`labels(actor_ids) -> {actor_id: {...}}`, shown as given. meydan has no word for them.
