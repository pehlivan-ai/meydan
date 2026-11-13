# Changelog

What changed in each release. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/) (before 1.0, a minor version may change
tables or APIs; the entry says how to upgrade).

## [Unreleased]

## [0.18.0] - 2026-11-13
### Added
- `meydan.channels.telegram`: Telegram as a second channel. `Telegram([tokens], render=)` sends
  text, locations and template messages (rendered by the app into text and inline buttons);
  `parse_messages` turns updates into `Inbound`, a button press in the same shape as WhatsApp's;
  `poll` reads a bot by long polling and moves on only once a batch is stored
  ([docs/telegram.md](docs/telegram.md)). Tested against a fake Bot API, and text, buttons and presses against Telegram.
### Upgrading
- Nothing to migrate. An app that does not use Telegram is unaffected.

## [0.17.0] - 2026-11-06
### Added
- `Auth(received=...)`: an option for the sign-in by a code the person sends themselves
  (`POST/GET /sign-in`). Default `True`: both ways in, as before. An app that wants only sent codes
  (`/sign-in/code`, `/sign-in/verify`) can set it to `False`.
### Upgrading
- Nothing to do.

## [0.16.0] - 2026-11-05
### Added
- `Outbound.location_message(lat, lng, place=, address=)`: a map pin (WhatsApp `type: location`).
  Its `text` holds the place and address, so transcripts show what was sent. Coordinates outside
  WGS 84 raise `ValueError`.
### Upgrading
- Nothing to migrate: the outbox stores messages as JSON, and older payloads still load.

## [0.15.0] - 2026-11-05
### Added
- Sign in by a code we send ([docs/auth.md](docs/auth.md)): `start_code_sign_in` (same answer
  whether or not a code goes out; 3 sends per address per 15 minutes), `send_code` (an
  authentication template where the person talks to us, `ref="signin:<id>"`), `verify_code`
  (6 digits, 5 tries, salted per browser), `reject`. Routes `POST /auth/sign-in/code` and
  `POST /auth/sign-in/verify` when `Auth(parse_address=, send_code=)` are given.
- `sign_in_interceptor(on_reject=, rejected_reply=)`: WhatsApp's "I didn't request a code" button
  cancels the sign-in it answers.
- `Outbound.code_message` / `Outbound.otp`: authentication templates (WhatsApp: the code in the
  body and the copy-code button). The outbox blanks the code once sent (or dead).
### Changed
- Codes sent to the chat confirm only "received" sign-ins (a code we sent is typed in the browser).
### Upgrading
- `meydan_sign_ins`: new columns `method` (default "received"), `attempts`, `rejected_at` and an
  index on (identity_id, created_at): generate a migration in your app.

## [0.14.0] - 2026-11-03
### Added
- Endpoints ([docs/endpoints.md](docs/endpoints.md)): our own numbers and email addresses with
  their latest state (`meydan.runtime.endpoints`: `record`, `sync`, `endpoints`, `get`), read as
  checks (`ok` / `warn` / `fail`) whose worst is the endpoint's health.
- `WhatsAppCloud` number management: `list_numbers`, `number`, `add_number`, `request_code`,
  `verify_code`, `register`, `profile`, `set_profile` (errors as `GraphError`, never the token);
  `whatsapp.number_events(payload)`: accounts whose numbers changed, to sync.
- `meydan.channels.email.dns_health`: MX, SPF, DKIM, DMARC and DMARC reports of an address's
  domain (extra `meydan[email]`, dnspython).
- `python -m meydan.cli`: `numbers list|add|request-code|verify|register|profile|copy-profile`,
  `email check`.
- `observe.query.endpoints` and `GET /observe/endpoints` (visibility item `operations`).
### Upgrading
- New table `meydan_endpoints`: generate a migration in your app.

## [0.13.0] - 2026-11-03
### Added
- Expect and ask ([docs/expect.md](docs/expect.md)): a stored expectation that an incoming
  answer completes once (`pending -> answered | expired | cancelled | undeliverable`).
  `expect(...)` records it (token hashed, `key` + `on_pending`: refuse / resend / allow);
  `ask(...)` sends a template to an address (`plan_contact`) with buttons carrying the token and
  `ref="expect:<id>"` (followed by `interactions` and the engagement scale).
- `Expectations(tables, {kind: Handler})`: `Runtime(expect=...)` matches answers before other
  interceptors (token, the asking message, the address's owner, in time, a valid option; one
  answer) and sweeps overdue ones every `RuntimeConfig.expect_sweep_s` (`Handler.on_close`).
- README: the invite recipe is now `ask` + a handler (half the code).
### Upgrading
- New table `meydan_expectations` (+ two partial indexes): generate a migration in your app.

## [0.12.1] - 2026-11-03
### Changed
- `meydan.runtime.core` (760 lines) split by responsibility, behaviour unchanged: `types`
  (channel, turn, interceptor, config), `inbound` (receive, interceptors, receipts,
  `replied_to`), `outbound` (enqueue, sender, `backoff`), `worker` (seen, turns), `conversation`
  (`Said`, `transcript`). `Runtime` is the one face over them; every name importable before still
  is (`meydan.runtime`, `meydan.runtime.core`). The test suite passes unchanged.

## [0.12.0] - 2026-11-03
### Added
- Engagement: one ordinal scale for every message we send, on every channel
  (`observe.engagement`): 0 none, 1 delivered, 2 seen, 3 answered; `engagement(..., within=)`
  derives the level from kept times at any horizon (nothing stored). Undeliverable messages are
  off the scale. `SEEN_BIAS` says how a channel's "seen" leans (WhatsApp `under`, email `over`).
- `interactions(...)` covers any message, not only templates: by `conversation_id` or
  `turn_id` too, with `engagement`, `answered_at`, `answer` (explicit: it points at the
  message; implicit: they wrote before our next message), `undeliverable`, `seen_bias`, and a
  `within` horizon.
- README: the engagement table (levels per channel).
### Upgrading
- No table changes.

## [0.11.0] - 2026-11-02
### Added
- Following a message we sent ([docs/observe.md](docs/observe.md)):
  - `Outbound.ref`: the app's own key for what a message is about (e.g. `invite:<id>`), kept on
    the outbox row (`text_message(..., ref=)`, `template_message(..., ref=)`).
  - Receipt times on the outbox: `delivered_at` and `read_at`, from the first receipt that
    showed them, in any order (a read counts as a delivery).
  - `Inbound.reply_to`: the id of our message an answer points at (WhatsApp `context.id`,
    parsed by `parse_messages`), stored on the inbox row; `replied_to(conn, tables, message)`
    returns the outbox row and `ref` it answers (`Replied`).
  - Read model `observe.query.interactions(...)` by `ref`, `ref_prefix` or `template`: status,
    times and answers, with visibility rules (button titles always, free text only with
    `message_text`, audited).
### Upgrading
- New columns: `meydan_inbox.reply_to`, `meydan_outbox.ref`, `delivered_at`, `read_at`, and two
  partial indexes. Generate a migration in your app (all nullable: the previous version keeps
  working against the new tables).

## [0.10.0] - 2026-11-02
### Added
- `identity.plan_contact(...)` for contacts we start (invites, reminders to new numbers): settles
  nothing; an unknown address stays unowned, with a conversation of its own, until the channel
  shows whose it is. Returns `Planned(conversation_id, identity_id, actor_id | None, ...)`.
- Receipts say who received a message: `StatusUpdate.recipient` (WhatsApp `recipient_id` +
  `recipient_user_id`), and `runtime.receive` corroborates them (`identity.corroborate`): an
  unowned phone joins the person already known by that BSUID. A receipt creates nobody and merges
  nobody (two owners: `proposed`). `parse_statuses(payload, portfolio=...)` scopes BSUIDs.
- `shared_contacts(message)` for contact cards someone sends (`SharedContact`, `SharedPhone`
  with `.address` when WhatsApp vouches for the number); the message text carries names only.
- `identity.find(...)`: an address's identity without creating one.
- Conversation lists show the name we gave an address nobody owns yet.
### Changed
- The invite pattern in the README uses `plan_contact` and checks the address's owner on the
  press, not the actor stored at sending.
### Upgrading
- No table changes. Pass `portfolio=` to `parse_statuses` if you pass it to `parse_messages`.
- If you started contacts with `route(...)`, switch to `plan_contact(...)` so an unknown number
  doesn't become a new person before they answer.

## [0.9.0] - 2026-10-30
### Added
- Templates: meydan keeps each template's state from the channel (review, pauses, category,
  quality) and the sender checks it: approved or unknown is sent; waiting (PENDING, PAUSED, ...) is
  held out of the conversation's line and sent once the template is sendable again; rejected or
  disabled is given up with the reason ([docs/templates.md](docs/templates.md)).
- `parse_template_updates(payload)` for the three WhatsApp template webhooks; `runtime.receive(...,
  template_updates)`; `WhatsAppCloud.list_templates(account)` and `templates.sync(...)` for a sync
  at startup; read model `observe.query.templates` and `GET /observe/templates`.
### Changed
- `Outbound.template_message(template, language, /, ...)`: the first two arguments are positional,
  so a template parameter may be called `name`.
### Upgrading
- New table `meydan_templates`, and the outbox status `held`: generate a migration in your app
  (the outbox status CHECK constraint changes; autogenerate may not see that, so add it by hand).

## [0.8.0] - 2026-10-29
### Added
- Template buttons that answer back: quick-reply buttons carry a payload you choose when sending
  (`Outbound.template_message(..., buttons=("accept:…", "decline:…"))`), and
  `reply_payload(message)` reads it from the press, so an invite or a confirmation can be matched
  to the person who answered.

## [0.7.0] - 2026-10-29
### Added
- Visibility: what a signed-in person sees (message text, addresses, agent details, costs,
  everyone's conversations or only their own), chosen in one place: `Visibility.need_to_know()`
  (the default) or `Visibility.trusted(scope)` for a small team, then per item. The read models
  apply it inside their queries ([docs/visibility.md](docs/visibility.md)).
### Changed
- Read models and `observe.web.routes(auth, visibility)` take a `visibility`; money now needs the
  `costs` item everywhere (overview, conversations, turns), and phone numbers can be shown in full
  to people with `addresses`.

## [0.6.1] - 2026-10-29
### Fixed
- If OpenFGA lost its data, `Access.start()` creates a new store and rebuilds every open
  assignment from your grants; restarting closes the old client.

## [0.6.0] - 2026-10-29
### Added
- Access: who may do what, on what, with OpenFGA (Apache-2.0) as the engine. Roles live in your
  model file, assignments are rows in your database mirrored to OpenFGA through an outbox, and
  relations already in your data are passed with each question instead of being copied
  ([docs/access.md](docs/access.md)).

## [0.5.0] - 2026-10-29
### Added
- Sign-in by sending a short code from an address meydan already knows (no passwords, no
  templates); sessions and scopes are deny-by-default and stored only as hashes
  ([docs/auth.md](docs/auth.md)).
- Read models (overview, conversations, transcripts with each turn's timeline, costs, problems)
  with an HTTP surface; others' message text is redacted unless allowed, and such reads are audited.
- Interceptors: messages handled before the agent (e.g. sign-in codes).

## [0.4.0] - 2026-10-29
### Added
- Observe: every turn leaves a record (timing, status, tokens and what it cost, exact where the
  provider reports it), written with the reply; one line in your agent reports model usage
  ([docs/observe.md](docs/observe.md)).

## [0.3.1] - 2026-10-28
### Added
- Presence: "seen" about a second after a message arrives, then "typing…" when the reply starts,
  refreshed while a slow reply is still being made; one setting (`presence="human" | "instant" |
  "quiet"` or numbers), and a measurable timeline per turn ([docs/presence.md](docs/presence.md)).

## [0.3.0] - 2026-10-27
### Added
- Who is who: people, agents and organizations are **actors**; their addresses (phone numbers,
  WhatsApp business-scoped user ids, email later) are **identities**, linked only on evidence
  through append-only **claims** ([docs/identity.md](docs/identity.md)).
- A conversation has its own id, so it survives a person hiding or sharing their number; replies
  are one **turn** of one or more messages; `transcript()` reads a conversation on one clock.
### Upgrading
- The tables change: backfill old rows with `meydan.runtime.identity.route` (one rule for old and
  new messages).

## [0.2.1] - 2026-10-27
### Fixed
- If the "typing…" indicator fails, the reply still goes out; errors that retrying can't fix (a
  wrong token, a malformed request) are marked permanent right away.

## [0.2.0] - 2026-10-27
### Added
- The channel and the runtime: meydan holds real WhatsApp conversations, and you only write what
  the agent says back, `reply(turn)`.

## [0.1.0] - 2026-10-27
### Added
- `meydan.llm` (models from spec strings, fallbacks, one retry layer), `meydan.clock`,
  `meydan.tools`, and the instance template (Compose, CI/CD, digest deploy with rollback).

[0.9.0]: https://github.com/pehlivan-ai/meydan/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/pehlivan-ai/meydan/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/pehlivan-ai/meydan/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/pehlivan-ai/meydan/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/pehlivan-ai/meydan/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/pehlivan-ai/meydan/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/pehlivan-ai/meydan/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/pehlivan-ai/meydan/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/pehlivan-ai/meydan/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/pehlivan-ai/meydan/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/pehlivan-ai/meydan/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/pehlivan-ai/meydan/releases/tag/v0.1.0
