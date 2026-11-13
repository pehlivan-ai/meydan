# Getting started

Two ways to work with meydan. Pick the one you are doing now.

| You are | You want | Go to |
|---|---|---|
| an **app developer** | an app that answers people on WhatsApp, built from meydan's parts | [Build an app](#build-an-app) |
| a **framework developer** | to change meydan itself | [Work on meydan](#work-on-meydan) |

Both need [uv](https://docs.astral.sh/uv/) and a Postgres 18 (Docker, or a local one).

## Build an app

You write one function, `reply(turn)`. The template is the rest: webhook, runtime loops,
migrations, image, deploy.

### 1. Copy the template and run it (no WhatsApp needed)

```
cp -R meydan/templates/instance myapp && cd myapp
mv gitignore .gitignore && cp .env.example .env
uv sync                                # installs meydan at the pinned release; commit uv.lock
docker compose up -d db                # Postgres 18 on localhost:5432
uv run alembic upgrade head            # meydan's tables
uv run python try.py "merhaba"         # app: You wrote: merhaba
uv run pytest                          # the plumbing, end to end
```

With a Postgres of your own, set `DATABASE_URL` in `.env` instead of starting `db`.

### 2. Make it yours

| File | What you do there |
|---|---|
| `app/agent.py` | `reply(turn)`: what your app says. `turn.actor_id` is who wrote |
| `app/tables.py` | your tables, on the same metadata as meydan's; then `uv run alembic revision --autogenerate -m "..."` |
| `app/config.py` | settings your app reads from `.env` |
| `app/main.py` | where parts are built once and passed on (an `Expectations`, a model, your services) |

Run `try.py` after every change: it is your app with a fake channel, so nothing you try
reaches a person.

### 3. Let it talk to people

Get what the [bring-up list](bring-up.md) names (rows 1 to 8), put the values in `.env`, then:

```
uv run uvicorn --factory app.main:create_app      # port 8000
cloudflared tunnel --url http://localhost:8000    # a public address while trying
```

Give Meta `https://<tunnel>/webhooks/whatsapp` and your `WHATSAPP_VERIFY_TOKEN`, subscribe to
`messages`, and write to your number. Use a dev app and a test number for this, never the ones
real people write to ([dev and prod](bring-up.md#dev-and-prod)).

### 4. Put it on a VM

When people depend on it: [Setting up an instance](../README.md#setting-up-an-instance). CI
builds the image once, the VM runs it by digest, migrations run as their own step, and an
unhealthy release rolls back.

### Then

| You need | Read |
|---|---|
| to know who wrote, across numbers and usernames | [identity.md](identity.md) |
| to ask something and wait for the answer (buttons) | [expect.md](expect.md) |
| to write first, outside the 24-hour window | [templates.md](templates.md) |
| "seen" and "typing…" | [presence.md](presence.md) |
| your own numbers and their health | [endpoints.md](endpoints.md) |
| what each turn cost, and what was said | [observe.md](observe.md) |
| people signing in to a console | [auth.md](auth.md), [access.md](access.md), [visibility.md](visibility.md) |

## Work on meydan

```
git clone https://github.com/pehlivan-ai/meydan && cd meydan
uv sync --all-extras
docker run -d --name meydan-pg -p 5432:5432 -e POSTGRES_USER=meydan -e POSTGRES_PASSWORD=meydan \
  -e POSTGRES_DB=meydan_test postgres:18
uv run pytest                # 118 pass; 7 need OpenFGA and are skipped without it
uv run ruff check . && uv run ruff format --check .
```

- Another Postgres 18: `MEYDAN_TEST_DATABASE_URL=postgresql+psycopg://user:pass@host/db`.
- The access tests: `docker run -d --name meydan-fga -p 18080:8080 openfga/openfga:v1.22.0 run`.
- Rules for a change: [CONTRIBUTING.md](../CONTRIBUTING.md). Design: [architecture.md](architecture.md).

### Changing meydan and an app together

```
cd myapp
uv add --editable ../meydan            # the app now runs your working copy
# ... change both, run both test suites ...
```

Before the app merges: release a tag in meydan, then point the app back at it
(`rev = "vX.Y.Z"` under `[tool.uv.sources]` in its `pyproject.toml`) and `uv sync`.

### What belongs in meydan

A pattern moves in once several real apps need it. Until then it stays in the app. Nothing
that names an app, a client, a domain or a number goes into this repository, in code, tests or
docs.

### The template

`templates/instance` is itself tested by copying it and running step 1 above. Do that after
changing anything in it: meydan's own `uv run pytest` does not run the template's tests.
