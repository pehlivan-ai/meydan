<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/readme/hero-dark.webp">
  <img alt="An isometric low-poly town square: people in small groups, their messages arcing across the square to a planning table by the fountain." src="docs/assets/readme/hero-light.webp">
</picture>

<h1 align="center">meydan</h1>

<p align="center">
  <em>Meydan</em> is Turkish for the town square: the open place where people meet, talk and make plans.
</p>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-v0.2.1-1a9e8f">
  <img alt="python" src="https://img.shields.io/badge/python-3.12%2B-3776ab">
  <img alt="license" src="https://img.shields.io/badge/license-Apache--2.0-d98b1f">
</p>

meydan is a Python framework and a set of instance primitives for running **personal agents**
(one per person, acting on their behalf over a messaging channel such as WhatsApp) and
**planning agents** that coordinate people one-to-one and in groups: finding partners, forming
groups, agreeing on a time and place, filling a spot when someone drops out.

Each deployment is an **instance**: one community, its own data, its own VM. Instances are
designed to become peers in a network later (agent-to-agent, A2A-shaped messages).

> Status: early (v0.2.1). Only the pieces marked *available* below exist today. The rest is the
> plan, documented in [docs/architecture.md](docs/architecture.md).

## What v0.2 lets you do

Before v0.2, meydan could talk to a model and say dates right, but it couldn't hold a
conversation on WhatsApp. Now you write one function, what your agent says back, and meydan
takes care of the rest:

```python
async def reply(turn: Turn) -> list[Outbound]: ...
```

- **Nothing is handled twice.** WhatsApp may deliver the same message more than once; it is
  processed once.
- **People can type the way they type.** Three quick messages in a row are read together as
  one turn, in the order they were written.
- **Nothing gets lost.** Replies are saved together with the conversation state, so a crash
  between "decided" and "sent" can't drop a message.
- **Replies feel human and arrive.** They go out in order, at a natural pace, with retries;
  what still can't be delivered is set aside for you to see, not silently dropped.

**v0.2.1** makes two edges safe: if the "typing…" indicator fails, the reply still goes out;
and errors that retrying can't fix (a wrong token, a malformed request) are marked as permanent
right away instead of being retried.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/readme/flow-dark.svg">
  <img alt="Runtime flow: Webhook, Inbox, Actor, Outbox, Sender. Three messages from one person enter the actor and one reply leaves it." src="docs/assets/readme/flow-light.svg" width="100%">
</picture>

## What's in the box

<table>
  <tr>
    <td align="center" width="33%"><img src="docs/assets/readme/mod-llm.webp" width="112" alt=""><br><b><code>meydan.llm</code></b><br><sub>Models from spec strings, fallback chain, backpressure, one retry layer</sub><br><sub>✅ available</sub></td>
    <td align="center" width="33%"><img src="docs/assets/readme/mod-clock.webp" width="112" alt=""><br><b><code>meydan.clock</code></b><br><sub>One place that converts time and writes dates for people</sub><br><sub>✅ available</sub></td>
    <td align="center" width="33%"><img src="docs/assets/readme/mod-tools.webp" width="112" alt=""><br><b><code>meydan.tools</code></b><br><sub>Framework-agnostic tool contract, identity from the channel</sub><br><sub>✅ available</sub></td>
  </tr>
  <tr>
    <td align="center"><img src="docs/assets/readme/mod-runtime.webp" width="112" alt=""><br><b><code>meydan.runtime</code></b><br><sub>Inbox → actor per conversation → outbox → sender, on Postgres</sub><br><sub>✅ available · new in v0.2</sub></td>
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
| `meydan.runtime` | Inbox (idempotent on message id) → one actor per conversation (advisory lock, debounce, resequencing, poison-message guard) → transactional outbox → sender (per-conversation order, bubble pacing, jittered retries, dead letter, receipt correlation). Postgres 18 | available (`meydan[runtime]`) |
| `meydan.channels.whatsapp` | WhatsApp Cloud API: signature check, webhook parsing (text, buttons, interactive, receipts) routed by `phone_number_id`, sending text/templates from the number written to, typing indicator, retryable vs permanent errors | available (`meydan[runtime]`) |
| planning | Plan state machine, group consensus rules, matching, waitlist/backfill, reliability, consent | planned |
| peers | Typed agent-to-agent messages between roles and, later, between instances | planned |
| simulation | Synthetic personas and scenarios to evaluate agents before they meet people | planned |

</details>

## Install

meydan is consumed as a pinned git dependency:

```
uv add "meydan[llm,runtime] @ git+https://github.com/pehlivan-ai/meydan@v0.2.1"
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

## Runtime in an instance

```python
from meydan.channels.whatsapp import WhatsAppCloud, parse_messages, parse_statuses, verify_signature
from meydan.channels.base import Outbound
from meydan.runtime import Runtime, Turn, define_tables

runtime_tables = define_tables(Base.metadata)  # your Alembic migrates meydan_inbox/outbox


async def reply(turn: Turn) -> list[Outbound]:  # your personal agent
    return [Outbound.text_message(f"got: {turn.text}")]


runtime = Runtime(engine, runtime_tables, WhatsAppCloud(access_token), reply)

# webhook: verify, then store in the request's transaction and answer 200 right away
with engine.begin() as conn:
    runtime.receive(conn, parse_messages(payload), parse_statuses(payload))

# background (e.g. FastAPI lifespan): worker + sender loops
await runtime.run(stop_event)
```

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

## Development

```
uv sync --all-extras
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## License

Apache-2.0. See [LICENSE](LICENSE).
