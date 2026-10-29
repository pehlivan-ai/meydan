# Changelog

What changed in each release. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/) (before 1.0, a minor version may change
tables or APIs; the entry says how to upgrade).

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
