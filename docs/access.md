# Access: who may do what, on what

Available since v0.6.0 (`meydan.access`, extra `access`).

meydan doesn't implement an authorization engine. It uses **OpenFGA** (Apache-2.0, the open
implementation of Google's Zanzibar model) and adds the glue around it.

## Roles in code, assignments as data
- **Your model file** (`access.fga`) defines types, roles and permissions in one readable place:
  resource by resource, permissions from roles, roles from other roles, inheritance over relations.
  Test it with the OpenFGA CLI (`fga model test`) and convert it to JSON for meydan
  (`fga model transform --output-format json`).
- **Assignments** ("Ayşe manages space:yoga") are rows in `meydan_grants`: your database is the
  source of truth, and every change is audited. meydan mirrors them to OpenFGA through an outbox
  written **in the same transaction**, so the two can't drift apart.
- **Relations that already live in your data** (an event's space, a booking's owner) are not
  copied into OpenFGA: pass them with the question as contextual tuples.

```
model
  schema 1.1
type actor
type instance
  relations
    define owner: [actor]
    define admin: [actor] or owner
    define can_operate: admin
type space
  relations
    define instance: [instance]
    define manager: [actor] or admin from instance
    define can_edit: manager
type event
  relations
    define space: [space]
    define host: [actor]
    define can_edit: host or manager from space
```

## Using it
```python
from meydan.access import Access, AccessConfig, assign

access = Access(
    engine,
    runtime_tables,
    AccessConfig(
        api_url="http://openfga:8080",
        api_token=...,
        model="access/access.json",
        instance="instance:main",
        scopes={"ops": "can_operate"},  # roles -> meydan scopes
    ),
)
await access.start()  # store + model (rewritten only when it changed)
asyncio.create_task(access.run(stop))  # keeps the outbox synced

with engine.begin() as conn:
    assign(conn, runtime_tables, ayse, "manager", "space:yoga")  # data first

await access.check(
    ayse, "can_edit", "event:sunrise", context=[("space:yoga", "space", "event:sunrise")]
)  # one question
await access.list_objects(ayse, "can_edit", "space")  # what may they edit
```

With meydan.auth:
- `Auth(..., resolve_scopes=access.scopes)` turns platform roles into the scopes the read models
  use.
- `sign_in_interceptor(..., allowed=may_sign_in(tables, roles=("owner", "admin"),
  obj="instance:main"))` lets role holders sign in.

## Notes
- Mirroring is asynchronous (about a second): a check right after `assign` may still say no until
  `sync_once` ran.
- If OpenFGA loses its data (a new server, a wiped datastore), `start()` notices the missing store,
  creates a new one and rebuilds every open assignment from your grants: your database is the
  source of truth.
- Run OpenFGA on your internal network only, with a preshared key; its datastore can be your
  Postgres (a separate schema).
