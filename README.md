<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/readme/hero-dark.webp">
  <img alt="A small floating island settlement by the sea: people in small groups, their messages arcing to a shared table under an olive tree." src="docs/assets/readme/hero-light.webp">
</picture>

<h1 align="center">meydan</h1>

<p align="center">
  <em>Meydan</em> is Turkish for the town square: the open place where people meet, talk and make plans.
</p>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-v0.8.0-1a9e8f">
  <img alt="python" src="https://img.shields.io/badge/python-3.12%2B-3776ab">
  <img alt="license" src="https://img.shields.io/badge/license-Apache--2.0-d98b1f">
</p>

## Why

You post a plan in the WhatsApp group. Everyone says they'll come. The plan never happens.

Nobody owns the decision, there is no deadline, and when one person drops out the whole thing
quietly dies. meydan is for agents that fix exactly that: each person has an agent they talk to
over messaging, and one planner owns the shared decision: it proposes concrete options, closes
them by a rule, asks before committing anyone, and fills the spot when someone drops out.

Underneath sits the part every WhatsApp agent needs and nobody wants to rebuild: a runtime that
receives every message once and in order, keeps track of who is who, and makes sure every reply
arrives. That part is available today; the planner is on the [roadmap](#roadmap).

## Imagine you live here

<table>
  <tr>
    <td align="center" width="96"><img src="docs/assets/readme/char-you.webp" width="80" alt=""></td>
    <td>You live on this island. A small light follows you around and acts for you.</td>
    <td>your <b>personal agent</b></td>
  </tr>
  <tr>
    <td align="center" width="96"><img src="docs/assets/readme/char-tune.webp" width="80" alt=""></td>
    <td>You decide how much it does on its own: <i>ask me first</i>, or <i>just handle it</i>.</td>
    <td><b>tunable</b><br><sub>planned</sub></td>
  </tr>
  <tr>
    <td align="center" width="96"><img src="docs/assets/readme/char-garden.webp" width="80" alt=""></td>
    <td>Where you've named clearly what you want and you both agreed on it, it acts without being asked, the way you would think. You keep gardening that ground together, and it ripens with practice.</td>
    <td><b>proactive</b>, within what you agreed<br><sub>planned</sub></td>
  </tr>
  <tr>
    <td align="center" width="96"><img src="docs/assets/readme/mod-channels.webp" width="80" alt=""></td>
    <td>You talk to it by message, like to a friend.</td>
    <td>a <b>channel</b><br><sub>WhatsApp</sub></td>
  </tr>
  <tr>
    <td align="center" width="96"><img src="docs/assets/readme/mod-runtime.webp" width="80" alt=""></td>
    <td>Every message reaches it once and in order. Every reply arrives.</td>
    <td>the <b>runtime</b></td>
  </tr>
  <tr>
    <td align="center" width="96"><img src="docs/assets/readme/mod-llm.webp" width="80" alt=""></td>
    <td>It thinks with a model. If one goes dark, the next is already lit.</td>
    <td><b>llm</b> with fallback</td>
  </tr>
  <tr>
    <td align="center" width="96"><img src="docs/assets/readme/mod-planning.webp" width="80" alt=""></td>
    <td>Your neighbors have lights too. When they need to agree on a time and place, one planner sets the table.</td>
    <td>the <b>planner</b><br><sub>planned</sub></td>
  </tr>
  <tr>
    <td align="center" width="96"><img src="docs/assets/readme/mod-instance.webp" width="80" alt=""></td>
    <td>The island is one community. Later, bridges to other islands.</td>
    <td>an <b>instance</b>, later <b>peers</b></td>
  </tr>
</table>

## In plain terms

meydan is a Python framework and a set of instance primitives for running **personal agents**
(one per person, acting on their behalf over a messaging channel such as WhatsApp) and
**planning agents** that coordinate people one-to-one and in groups: finding partners, forming
groups, agreeing on a time and place, filling a spot when someone drops out.

Each deployment is an **instance**: one community, its own data, its own VM. Instances are
designed to become peers in a network later (agent-to-agent, A2A-shaped messages).

> Status: early (v0.8.0). Only the pieces marked *available* below exist today. The rest is the
> plan, documented in [docs/architecture.md](docs/architecture.md).

**New in v0.2:** the channel and the runtime. meydan can now hold real WhatsApp
conversations, and you only write what the agent says back, `reply(turn)`.

**v0.2.1:** if the "typing…" indicator fails, the reply still goes out; errors that retrying
can't fix (a wrong token, a malformed request) are marked permanent right away.

**New in v0.3:** who is who. People, agents and organizations are **actors**; their addresses
(phone numbers, WhatsApp business-scoped user ids, email later) are **identities**, linked only
on evidence through append-only **claims** ([docs/identity.md](docs/identity.md)). A
conversation has its own id, so it survives a person hiding or sharing their number; replies
are one **turn** of one or more messages; `transcript()` reads a conversation on one clock.
Upgrading a v0.2 instance changes the tables: backfill old rows with
`meydan.runtime.identity.route` (one rule for old and new messages).

**v0.3.1:** presence. "Seen" about a second after a message arrives, then "typing…" when the reply
starts, refreshed while a slow reply is still being made; one setting (`presence="human" |
"instant" | "quiet"` or numbers), and a measurable timeline per turn ([docs/presence.md](docs/presence.md)).

**v0.4.0:** observe. Every turn leaves a record (timing, status, tokens and what it cost, exact where
the provider reports it), written with the reply; one line in your agent reports model usage
([docs/observe.md](docs/observe.md)).

**v0.5.0:** sign-in and read access. People sign in by sending a short code from the address meydan
already knows (no passwords, no templates); sessions and scopes are deny-by-default and stored only
as hashes ([docs/auth.md](docs/auth.md)). Principal-scoped read models (overview, conversations,
transcripts with each turn's timeline, costs, problems) with an HTTP surface; others' message text
is redacted unless allowed, and such reads are audited. New: interceptors (messages handled before
the agent).

**v0.6.0:** access. Who may do what, on what, with OpenFGA (Apache-2.0) as the engine: roles live in
your model file, assignments are rows in your database mirrored to OpenFGA through an outbox, and
relations already in your data are passed with each question instead of being copied
([docs/access.md](docs/access.md)).

**v0.7.0:** visibility. What a signed-in person sees (message text, addresses, agent details, costs,
everyone's conversations or only their own), chosen in one place: `Visibility.need_to_know()` (the
default) or `Visibility.trusted(scope)` for a small team, then per item; the read models apply it
inside their queries ([docs/visibility.md](docs/visibility.md)).

**v0.8.0:** template buttons that answer back. A template's quick-reply buttons carry a payload you
choose when sending (`Outbound.template_message(..., buttons=("accept:…", "decline:…"))`), and
`reply_payload(message)` reads it from the press, so an invite or a confirmation can be matched to
the person who answered.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/readme/flow-dark.svg">
  <img alt="Runtime flow: Webhook, Inbox, Actor, Outbox, Sender. Three messages from one person enter the worker and one reply leaves it." src="docs/assets/readme/flow-light.svg" width="100%">
</picture>

## What's in the box

<table>
  <tr>
    <td align="center" width="33%"><img src="docs/assets/readme/mod-llm.webp" width="112" alt=""><br><b><code>meydan.llm</code></b><br><sub>Models from spec strings, fallback chain, backpressure, one retry layer</sub><br><sub>✅ available</sub></td>
    <td align="center" width="33%"><img src="docs/assets/readme/mod-clock.webp" width="112" alt=""><br><b><code>meydan.clock</code></b><br><sub>One place that converts time and writes dates for people</sub><br><sub>✅ available</sub></td>
    <td align="center" width="33%"><img src="docs/assets/readme/mod-tools.webp" width="112" alt=""><br><b><code>meydan.tools</code></b><br><sub>Framework-agnostic tool contract, identity from the channel</sub><br><sub>✅ available</sub></td>
  </tr>
  <tr>
    <td align="center"><img src="docs/assets/readme/mod-runtime.webp" width="112" alt=""><br><b><code>meydan.runtime</code></b><br><sub>Inbox → worker → outbox → sender, identities, on Postgres</sub><br><sub>✅ available · identities new in v0.3</sub></td>
    <td align="center"><img src="docs/assets/readme/mod-channels.webp" width="112" alt=""><br><b><code>meydan.channels.whatsapp</code></b><br><sub>WhatsApp Cloud API: webhooks in, messages out</sub><br><sub>✅ available · new in v0.2</sub></td>
    <td align="center"><img src="docs/assets/readme/mod-instance.webp" width="112" alt=""><br><b><code>templates/instance</code></b><br><sub>Compose stack, CI/CD, digest deploy with rollback</sub><br><sub>✅ available</sub></td>
  </tr>
  <tr>
    <td align="center"><img src="docs/assets/readme/mod-planning.webp" width="112" alt=""><br><b>planning</b><br><sub>Plan state machine, group consensus, matching, backfill</sub><br><sub>🧱 planned</sub></td>
    <td align="center"><img src="docs/assets/readme/mod-peers.webp" width="112" alt=""><br><b>peers</b><br><sub>Typed agent-to-agent messages, later between instances</sub><br><sub>🧱 planned</sub></td>
    <td align="center"><img src="docs/assets/readme/mod-simulation.webp" width="112" alt=""><br><b>simulation</b><br><sub>Synthetic personas and scenarios to test agents first</sub><br><sub>🧱 planned</sub></td>
  </tr>
</table>

<details>
<summary>Module details</summary>

| Module | What it does | Status |
|---|---|---|
| `meydan.llm` | Provider-agnostic models from spec strings (`openrouter:…`, `vertex:…@location`), fallback chain, per-provider concurrency limit with bounded queue, one retry layer (Retry-After, jittered exponential backoff), timeouts | available (`meydan[llm]`) |
| `meydan.clock` | One place that converts time: instance zone, injectable clock, localized date text, date windows at their precision ("Aralık 2026"), a day calendar for agent context | available |
| `meydan.tools` | Framework-agnostic tool contract: `(ToolContext, **params) -> model`, identity from the channel, `ToolError` for business rules | available |
| `templates/instance` | Docker Compose stack (Postgres 18, app, Caddy), CI/CD workflow, digest-pinned deploy with health check and rollback, one-time GCP setup | available |
| `meydan.runtime` | Inbox (idempotent on message id) → identities (actors, addresses, evidence-based claims; [docs/identity.md](docs/identity.md)) → worker, one turn per conversation at a time (advisory lock, debounce, resequencing, poison-message guard) → transactional outbox → sender (per-conversation order, bubble pacing, jittered retries, dead letter, receipt correlation). Postgres 18 | available (`meydan[runtime]`) |
| `meydan.channels.whatsapp` | WhatsApp Cloud API: signature check, webhook parsing (text, buttons, interactive, receipts) routed by `phone_number_id`, sending text/templates from the number written to, typing indicator, retryable vs permanent errors | available (`meydan[runtime]`) |
| `meydan.observe` | One record per turn and per model call (timing, tokens, cost), written with the reply; read models (overview, conversations, transcripts, costs, problems) with an HTTP surface ([docs/observe.md](docs/observe.md)) | available (`meydan[api]` for HTTP) |
| `meydan.auth` | Sign in by sending a code from an address meydan knows; sessions and scopes, hashed, deny by default ([docs/auth.md](docs/auth.md)) | available (`meydan[api]`) |
| `meydan.access` | Roles in your OpenFGA model file, assignments as rows in your database, mirrored through an outbox ([docs/access.md](docs/access.md)) | available (`meydan[access]`) |
| `meydan.visibility` | What a signed-in person sees: two presets, then per item; applied inside the read models ([docs/visibility.md](docs/visibility.md)) | available |
| planning | Plan state machine, group consensus rules, matching, waitlist/backfill, reliability, consent | planned |
| peers | Typed agent-to-agent messages between roles and, later, between instances | planned |
| simulation | Synthetic personas and scenarios to evaluate agents before they meet people | planned |

</details>

## Quick start

meydan gives you parts, not a finished app. You write what only your app knows; meydan handles
what every messaging agent needs. You wire the parts together in one place in your app.

### 1. Add meydan's tables to yours

```python
from meydan.runtime import define_tables

runtime_tables = define_tables(Base.metadata)  # meydan_* tables, migrated by your Alembic
```

Your domain (members, spaces, events, bookings) lives next to them in the same Postgres. A
message and the reply it causes are written in the same transactions as your own data.

### 2. Write what your agent says

```python
async def reply(turn: Turn) -> list[Outbound]:
    # turn.actor_id is who wrote, whichever address they used. Key your data on it.
    return [Outbound.text_message(f"got: {turn.text}")]
```

Tools follow the `meydan.tools` contract: the person comes from the channel, never from the model.

### 3. Wire the parts in your app

Your app builds the parts once, where it starts, and passes them to whoever needs them:

```python
# yourapp/compose.py
def compose(settings: Settings) -> Parts:
    engine = create_engine(settings.database_url)
    runtime = Runtime(engine, runtime_tables, WhatsAppCloud(settings.whatsapp_token), reply)
    access = Access(
        engine,
        runtime_tables,
        AccessConfig(api_url=settings.openfga_url, model="access.json", instance="instance:main"),
    )
    return Parts(engine, runtime, access)


# yourapp/main.py
def create_app(settings: Settings | None = None) -> FastAPI:
    parts = compose(settings or Settings())
    app = FastAPI(lifespan=...)  # starts parts.runtime.run(stop) next to the API
    app.include_router(webhook_routes(parts.runtime))
    return app
```

meydan has no app object, no registry and no global settings. Nothing connects anywhere because a
module was imported, so tests call `compose` with test settings and fakes, and two apps can use
meydan side by side.

### 4. Run it

```
uvicorn --factory yourapp.main:create_app
```

WhatsApp messages now arrive through your webhook, your `reply` answers them, and meydan delivers
the answer. An admin service is a second FastAPI app built the same way, with `meydan.auth`,
`meydan.access` and the `meydan.observe` read models.

### What you write, what meydan handles

| You write | meydan handles |
|---|---|
| your tables and business rules | every message once, in order; who wrote (actors, identities) |
| your agent: prompt and tools | one turn per conversation at a time; replies paced, retried, delivered and read |
| your access model (`access.fga`) | sign-in by a code sent from WhatsApp; sessions; role checks through OpenFGA |
| one visibility choice (`Visibility.trusted("ops")`) | what each person sees of conversations, texts, numbers and costs; audited reads |
| your screens, if you have any | read models with an HTTP surface: overview, conversations, costs, problems |

### How the parts connect

```
WhatsApp ─▶ your webhook ─▶ runtime.receive ─▶ inbox            (stored first, answered 200)
                                  │
                                  ▼
                      who wrote? (actor, identities)
                                  │
                                  ▼
            worker: one turn per conversation ─▶ your reply(turn) ─▶ outbox ─▶ sender ─▶ WhatsApp
                                  │                    │
                                  ▼                    ▼
                       observe: turn record     your tools: your tables

Browser ─▶ auth (session) ─▶ access (roles → scopes) ─▶ visibility ─▶ read models ─▶ Postgres
```

### Next steps

- Who is who: [docs/identity.md](docs/identity.md)
- Seen and typing: [docs/presence.md](docs/presence.md)
- Turn records and costs: [docs/observe.md](docs/observe.md)
- Sign-in: [docs/auth.md](docs/auth.md)
- Roles: [docs/access.md](docs/access.md)
- What people see: [docs/visibility.md](docs/visibility.md)

A pattern moves into meydan when several real apps need it. Until then, a few clear lines in your
app beat a framework that guesses your shape.

## Install

meydan is consumed as a pinned git dependency:

```
uv add "meydan[llm,runtime,api,access] @ git+https://github.com/pehlivan-ai/meydan@v0.8.0"
```

For working on meydan and an instance at the same time, clone it next to the instance and point
the instance at it temporarily:

```
git clone git@github.com:pehlivan-ai/meydan.git ../meydan
uv add --editable ../meydan          # local co-development
# before merging the instance: back to a released tag
uv add "meydan[llm] @ git+https://github.com/pehlivan-ai/meydan@vX.Y.Z"
```

Docker and CI build from the instance repo alone, so don't symlink meydan into it.

## Quick look

```python
from meydan.clock import TR, Clock
from meydan.llm import LLM, LLMSettings

clock = Clock("Europe/Istanbul", TR)
clock.calendar(3)  # ['2026-10-26 26 Ekim Pazartesi', '2026-10-27 27 Ekim Salı', ...]

llm = LLM(
    LLMSettings(
        model="openrouter:deepseek/deepseek-v4.1-flash",
        fallback_models=["openrouter:qwen/qwen3.8-flash"],
    )
)
model = llm.get_model()  # hand this to a Pydantic AI Agent
```

`LLMSettings` also reads `MEYDAN_LLM_*` environment variables (and `OPENROUTER_API_KEY`).

## Setting up an instance

1. Copy `templates/instance/` into your app repo (rename `gitignore` to `.gitignore` or merge it).
2. Your app serves `GET /health` on port 8000 and has a `Dockerfile`.
3. Create a VM with Docker Engine + Compose, clone your repo to the instance directory, put
   secrets in `.env` there.
4. Run `scripts/gcp-setup.sh` once (see its header), then fill the `env:` block of
   `.github/workflows/ci-cd.yml` with its output.
5. Push to `main`: CI runs, the image is built once, pushed, and deployed by digest. If the new
   version isn't healthy, the VM rolls back to the previous digest.

Keep migrations backward compatible with the previous release, or rollback can't work.

## Roadmap

The order things are expected to land in, and what each builds on. A module moves into meydan
once real instances need it; the order may change with what they need.

| Next | What | Builds on |
|---|---|---|
| 1 | **Booking**: offering → slot → booking (`proposed → held → confirmed → done / no_show / cancelled`), freed time offered to a waitlist in order | identities, clock |
| 2 | **Proactive messages**: reminders and offers on a schedule, the 24-hour window with template fallback, consent per purpose | outbox, templates, clock |
| 3 | **Outcome ledger**: which conversation led to which booking, and who confirmed it | booking, conversations |
| 4 | **Second channel** (Telegram), **handoff** to a human and back, calendar adapters | channel contract, runtime |
| 5 | **Relations** between actors (acts for, member of), linking addresses on proof | identity claims |
| 6 | **Planner**: plan state machine, group consensus, matching, backfill, reliability | 1–5 |
| 7 | **Agreed ground**: per person and per topic, what the agent may do alone | proactive messages, planner |
| 8 | **Simulation**: synthetic personas and scenarios to test planning rules before people | booking, planner |
| 9 | **Peers**: typed agent-to-agent (A2A) messages, between roles and then between instances | relations, planner |

## Tell us

meydan is shaped by the people who use it. If you plan things with groups, or build agents on
WhatsApp, [open an issue](https://github.com/pehlivan-ai/meydan/issues/new) and tell us:

- which plans fall apart in your groups, and at which step;
- what you would expect from an agent on WhatsApp, and what it should never do on its own;
- what was hardest when you set up a WhatsApp agent yourself.

<details>
<summary>Türkçe</summary>

WhatsApp grubuna bir plan yazıyorsunuz, herkes "gelirim" diyor ama plan bir türlü
gerçekleşmiyor. meydan bunun için: herkesin mesajla konuştuğu bir kişisel agent'ı, ortak kararı
ise tek bir planlayıcı üstleniyor. Somut seçenekler sunuyor, kararı bir kurala göre kapatıyor,
kimseyi sormadan bir şeye bağlamıyor, biri vazgeçince yerini dolduruyor.

Bugün hazır olan kısım, her WhatsApp agent'ının ihtiyaç duyduğu altyapı: her mesajı bir kez ve
sırasıyla alan, kimin kim olduğunu takip eden ve her cevabın ulaşmasını sağlayan bir runtime.
Planlayıcı ve agent'lar arası (A2A) mesajlaşma yol haritasında. Fikirlerinizi ve
deneyimlerinizi [issue](https://github.com/pehlivan-ai/meydan/issues/new) olarak yazın.

</details>

## Development

```
uv sync --all-extras
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## License

Apache-2.0. See [LICENSE](LICENSE).
