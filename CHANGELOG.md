# Changelog

What changed in each release. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/) (before 1.0, a minor version may change
tables or APIs; the entry says how to upgrade).

## [Unreleased]

## [0.52.2] - 2026-11-28
### Fixed
- `observe.query.conversations`: newest first by the last message either way (the time the row
  shows), not only by the person's last message; a message or template we sent moves the
  conversation up, and one we started without an answer is no longer at the bottom.

## [0.52.1] - 2026-11-27
### Added
- `Notices.bind(runtime, dues)`: notices built before the runtime (it takes its subscribers when
  built) get it afterwards; until then a fact waits.

## [0.52.0] - 2026-11-27
### Added
- `meydan.pay.notices` (experimental): a due's facts become messages to the person, once per
  (due, notice), right after the agent's reply when a turn is running in their conversation (at
  most `max_wait_s` later), at once otherwise. `Notice(on, name, card, skip=, at=)`, `opened`,
  `paid`, `before_deadline`; `Notices(...).subscribers()` (one per notice). Patterns from Novu
  (dedupe key, `skip`) and Inngest (debounce with a maximum wait, sleep until, cancel when paid).
- `meydan.pay.dues` writes facts (meydan.events) in the change's transaction: `due.opened` (once
  per due), `due.<state>` on each transition.
- `meydan.runtime.reach`: `reach(conn, tables, rules, actor_id)` (where a person last wrote: the
  conversation, our endpoint, their address, whether the reply window is open) and
  `busy(conn, tables, conversation_id)` (a message of theirs not answered yet).

### Upgrading
- No table changes. An app with dues now emits their facts; nothing subscribes until it adds
  notices.

## [0.51.1] - 2026-11-27
### Added
- `Dues(kind=)`: the kind its links' expectations are recorded as (default `pay`), so an app that
  had pay links of its own keeps them working.

## [0.51.0] - 2026-11-27
### Added
- `meydan.pay.dues` (experimental): what an actor owes for one of the app's subjects, until a
  deadline. `define_table(name="pay_dues")` (one open due per subject and payer); the app's
  `Subject` handler per kind (`payable` -> `Payable` / `NotPayable`, `on_paid`, `on_released`);
  `Dues.open` (the live one and the same link when asked again), `link` (the token kept sealed
  with meydan.vault, so the same link goes out again and old buttons keep working), `resolve`
  (open and in time, or paid until `readable_until`: the person's own paid page),
  `checkout_handler` (a checkout of `due:<id>` -> its due, each transition once: paid, late paid,
  refund unless another attempt pays it, withdrawn), `cancel`, `expire`.
- `meydan.runtime.expect.by_token(conn, tables, token, kind=)`: the expectation a token was made
  for, in any state.

### Upgrading
- New table when an app takes `meydan.pay.dues` (its migration adds it). Nothing else changes.

## [0.50.1] - 2026-11-27
### Fixed
- `meydan.pay.checkout.define_table`: the id is made by meydan (`meydan.ids.uuid7`), not by a
  `uuidv7()` server default, which exists only from Postgres 18 (meydan supports 16+).

### Upgrading
- No change for a table created earlier: its server default stays and is not used.

## [0.50.0] - 2026-11-27
### Added
- `meydan.pay` (experimental; `meydan[pay]`), taking a payment for something an app sells from a
  conversation, by its design in docs/pay.md. First part: `meydan.pay.checkout`, one attempt to
  pay through the seller's own provider. `define_table(metadata, prefix=, name="pay_checkouts")`;
  `Checkouts.open` (pending, or refused / error with a `Reason`; a used reference retried once),
  `apply` (a status applied once: an audit row and one call of the subject's handler per change),
  `apply_result` (the browser's result is a hint: the signature with the connection's `hmac`, then
  the status from the provider decides), `refresh`, `sweep` / `run` (open sessions often, paid
  ones daily until `watch_until`). The `Provider` protocol (`needs`, `consents`, `base_url`,
  `open`, `status`, `signed`), `requirements` and `clean` (what the provider needs against what is
  known; phones by libphonenumber), errors by status and code, never a provider's text.
- docs/pay.md: the plan (dues, notices that join the turn, the pay page API, the ledger).

### Upgrading
- New optional extra `pay`. An app that had its own copy of the checkouts table keeps it by
  passing `name=` (the columns, constraints and index names are the same).

## [0.49.0] - 2026-11-26
### Added
- Links: `Outbound.link_message(body, label, url)` (kind `link`): a text with one button that
  opens an https page. WhatsApp sends it as an interactive call-to-action URL button (`cta_url`;
  `whatsapp.check_link`: text up to 1024 characters, label up to 20), Telegram as an inline URL
  button, Instagram as text (`link_text`: the body, then `label: url`). Its `text` (what
  transcripts read) is the body and `[link: label]`, never the address.
- `meydan.reply.Link` attachment (`resolve` -> `LinkRef(url, label, text, recall, ref)`, or
  `Link.of(...)` from a row): the model names the thing, the app's code builds the address; its
  text form goes as the `fallback`.
- `Outbound.template_message(..., url_suffix=)`: a template's URL button variable (WhatsApp:
  button `sub_type` url, after the quick replies).
- `meydan.observe`: a link we sent shows as `Line.link` (`LinkPart`: label, url).
- `meydan.channels.base.is_https`.

### Changed
- `Outbound.from_payload` ignores keys it doesn't know (a row written by a newer release), and
  `to_payload` writes the new `url`, `url_label`, `url_suffix` only when set, so a rollback still
  sends what was queued.

### Upgrading
- No table changes. An app that renders observe lines by kind may show `link` (`line.link`).

## [0.48.0] - 2026-11-26
### Added
- Fact `conversation.read` (`meydan.operator.STAFF_READ`): `operator.mark_read` emits it, with the
  reader. Subscriber `StaffSeen` (meydan.runtime.ingest) marks what is still unread in that
  conversation "seen" on the channel, each message.

### Changed
- While a conversation is taken over, `Seen` no longer marks its messages read: the person writing
  sees "seen" when someone on the team opens the conversation (`operator.mark_read`, e.g. the
  operator route `POST /conversations/{id}/read`), not before nobody has read it.

### Upgrading
- No table changes. Mark a conversation read when a person on the team opens it (the operator read
  route, or `operator.mark_read`) so its writer sees "seen" during a takeover.

## [0.47.0] - 2026-11-26
### Added
- `meydan.events` (docs/events.md): facts and their subscribers. `emit(conn, tables, kind, key=,
  subject_id=, channel=, data=)` writes a fact in the caller's transaction and a delivery for each
  matching subscription; `Subscriber(name, kinds, handle, channel=, delay_s=, max_attempts=,
  lease_s=)`; `Events(engine, tables, subscribers).run(stop)` / `drain_once()`; handler outcomes
  None, `Later(seconds)`, `PermanentError`, any other error (backoff, then dead); `retry`,
  `unsubscribe`, `Events.rescue`, `prune`, `backlog`. Subscriptions are rows, so a subscriber in
  one process gets facts emitted in another.
- `Runtime(..., subscribers=[...])`, `Runtime.events`, `Runtime.events_once()`; `Health.events_s`
  and `events_dead` (counted in `ok`).
- `meydan.runtime.ingest`: a message is *ingested* once its content is in the system: text at once,
  a photo once `Ingest` (subscriber of `message.received`) has kept it. Facts `message.received`,
  `message.ingested`. `RuntimeConfig.ingest_wait_s` (20 s).
- `seen_kind`: an `unsupported` message reads as what WhatsApp says it was (`[album]`).

### Changed
- "Seen" is a subscriber of `message.ingested` (`Seen`), `read_after_s` after arrival: per message
  (an album's photos share one second, so "the latest" was not always the last), only once its
  content is in the system, and a refused read receipt is recorded and tried again.
- A turn waits until its messages are ingested (at most `ingest_wait_s`): a photo is fetched right
  after arrival by `Ingest`, no longer inside the turn.
- `WhatsAppCloud.mark_read` raises when WhatsApp refuses (it swallowed every error).

### Deprecated
- `Runtime.read_once`: runs `events_once` and warns; use `events_once`. To be removed in a later
  release.

### Removed
- The reader loop (`Worker.read_once`, `_due_reads`, `_set_read`) and the worker's in-turn fetch
  (`_keep_media`): replaced by the subscribers above.

### Upgrading
- New tables `<prefix>events`, `<prefix>event_subscriptions`, `<prefix>event_deliveries` and the
  column `<prefix>inbox.ingested_at` (nullable): generate a migration (expand only; the previous
  release runs on it). Optionally mark messages already answered as ingested:
  `UPDATE <prefix>inbox SET ingested_at = received_at WHERE processed_at IS NOT NULL`; unanswered
  older ones wait `ingest_wait_s` once, then go on.
- Nothing else to change: the runtime registers and runs its own subscribers. Give yours with
  `Runtime(subscribers=[...])`.

## [0.46.0] - 2026-11-26
### Added
- `meydan.channels.base.seen_kind(kind, payload)`: a photo sent "as a file" (a document whose
  `document.mime_type` is an image, on WhatsApp and Telegram) is a photo: fetched and kept like
  one (`fetch_media`), read as `[image] caption` in turns and transcripts, shown as an image by
  `meydan.observe`. Other documents are not downloaded.
- Telegram: messages with a document are kind `document` (were `unknown`); `download_media`
  fetches a document's file too.

### Upgrading
- No table changes.

## [0.45.0] - 2026-11-26
### Added
- Photos people send (docs/media.md, "Files people send us"): channels download them
  (`WhatsAppCloud.download_media`, `Telegram.download_media`; optional on the `Channel`
  protocol), the worker keeps them before the turn (`RuntimeConfig.fetch_media`, default
  `{"image"}`), `Inbound.media` / `Said.media` carry the key, and `meydan.observe` shows and serves
  them like the message they came in.
- `meydan.media.prepare_image` (upright, long edge `IMAGE_MAX_EDGE` = 1568 px, JPEG, metadata
  dropped; extra `media`: Pillow), `receive`, `forget_received(conn, tables, older_than)`.
- `RuntimeConfig.keep_received` (None: keep) with an hourly sweep (`media_sweep_s`,
  `Runtime.forget_received_once`); `image_max_edge`.
- `handler(..., images=True, history_images=N)`: the turn's photos go to the model as bytes
  (labelled when several), the last N of the history too; `history(lines, images)`;
  `AgentHandler.content(turn)`.
- Extra `media` (Pillow).

### Upgrading
- Table change: `<prefix>inbox.media` (text, nullable, FK to `<prefix>media.key` ON DELETE SET
  NULL). Add it with your migrations (an Alembic autogenerate picks it up).
- Nothing reaches a model until the app sets `images=True`; set it only for a model that reads
  images. Photos are fetched and kept by default (`fetch_media`); set `fetch_media=frozenset()` to
  keep only text, and `keep_received` to your retention.

## [0.44.0] - 2026-11-26
### Added
- Reactions (an emoji on a message): WhatsApp's `reaction` messages are parsed: `kind`
  "reaction" (`meydan.channels.base.REACTION`), `text` the emoji ("" when taken back),
  `reply_to` the channel id of the message it is on.
- `meydan.channels.base.describe(kind, text, target=None)`: what an incoming message reads as
  where its text alone would mislead or be empty: "[image] caption", "[audio]", a reaction as
  `[reaction 🙏 to "…"]` (`target`: that message's text), plain text as itself.
  `MEDIA_KINDS` lists the kinds whose text is only a caption.
- `RuntimeConfig.answer_reactions` (default False).

### Changed
- A reaction opens no turn: it is stored as consumed (`processed_at`) when it arrives, so nobody
  answers a 🙏. `answer_reactions=True` gives the handler a turn for it like any message.
- `transcript` reads incoming messages through `describe`: a photo is named, a reaction says
  which message it is on (ours: its recall, else its text). Lines that were empty (a photo
  without a caption, a voice note) are no longer dropped from an agent's history.
- `meydan.agent`: the default prompt of a turn reads each message through `describe`
  (`meydan.agent.said(turn)`); an app's own `said` is unchanged.

### Upgrading
- No table changes. An app that wants its agent to answer reactions sets
  `RuntimeConfig(answer_reactions=True)`.

## [0.43.0] - 2026-11-26
### Added
- Connections also hold what we need to call them (`meydan.bridge.connections`): credential
  kinds `token` (what we present to their API: a bearer token, an API key) and `hmac` (a secret
  they sign their results with; the algorithm and what is signed are the app's), usually
  **theirs**. `add_credential(conn, t, keyring, name, kind, value, *, origin="theirs", by=None,
  valid_until=None)` keeps one of any kind (1 to `VALUE_MAX` = 4096 characters, nothing blank
  around it; `whsec` and `whpk` checked as before). `value(conn, t, keyring, name, kind)` opens
  the newest live one; None if there is none or the connection is unknown or off (off stops
  both ways). `listing`, `reveal`, `retire` and `reseal` treat them like any credential.
  `meydan.runtime.tables.INBOUND_CREDENTIAL_KINDS` / `OUTBOUND_CREDENTIAL_KINDS`.
- `meydan.bridge.manage`: `POST /{name}/secrets` takes `{"kind": "token" | "hmac", "secret":
  "..."}`, theirs (never generated, not returned; `manage` as before; audited `token:theirs` /
  `hmac:theirs`). Listing shows them with a fingerprint; reveal is the same opt-in (`reveal=True`,
  audited).
### Fixed
- `connections.credentials` (what `Bridge` verifies requests with) reads only `whsec` and
  `whpk`; before, any other kind would have been taken as a public key. A `token` or an `hmac`
  never verifies a request to us (no earlier release could store one).
### Changed
- `POST /{name}/secrets`: `secret` may be up to 4096 characters (was 200); a `whsec` is still
  24 to 64 bytes.
### Upgrading
- The CHECK constraint on `meydan_connection_credentials.kind` (your prefix) gains `token` and
  `hmac`. Autogenerate does not see check constraints: replace it by hand in a migration.
  Allowed values: `'whsec', 'whpk', 'token', 'hmac'`. Its name: `kind` with a plain
  `MetaData()`; with the usual naming convention (`"ck": "ck_%(table_name)s_%(constraint_name)s"`)
  `ck_meydan_connection_credentials_kind` (check with `\d meydan_connection_credentials`):

  ```sql
  ALTER TABLE meydan_connection_credentials DROP CONSTRAINT ck_meydan_connection_credentials_kind;
  ALTER TABLE meydan_connection_credentials ADD CONSTRAINT ck_meydan_connection_credentials_kind
    CHECK (kind IN ('whsec', 'whpk', 'token', 'hmac'));
  ```

  (With Alembic ops: `op.drop_constraint(op.f(NAME), TABLE, type_="check")` and
  `op.create_check_constraint(op.f(NAME), TABLE, "kind IN (...)")`; `op.f` keeps a naming
  convention from renaming it again.) It only widens, so the previous release runs on it
  (expand). Existing rows pass, so `ADD ... NOT VALID` then `VALIDATE CONSTRAINT` works where a
  linter asks for it. A downgrade must delete `token`/`hmac` rows first.

## [0.42.0] - 2026-11-25
### Added
- `meydan.bridge.manage.routes(bridge, auth, may=, audit=, names=, reveal=False)`: managing
  connections over HTTP for a connection's own page or the team's. The app's `may(principal,
  connection, "read" | "manage")` decides everything (nothing by default, admins included);
  `audit` records each change in its transaction. List, get, events, refused requests, retry, a
  secret of ours (returned once) or theirs, public key, rotate, retire, on/off. Showing a stored
  secret again is off unless `reveal=True` (audited), so an app can keep secrets from its own
  admins.
- Refused requests of a known connection are kept (`meydan_bridge_rejections`: status, reason,
  webhook id; no bodies; pruned after 30 days by `Bridge.run`), so the other side can see why
  while setting up. `Bridge.note_rejection`, `Bridge.prune`.
### Upgrading
- New table `meydan_bridge_rejections`: generate a migration.

## [0.41.0] - 2026-11-25
### Added
- `meydan.bridge.connections` (extra `bridge`): connections and their credentials in the
  database instead of config. A credential is a shared secret (`whsec`, `v1`) or their public key
  (`whpk`, `v2a`), **ours** (we generated it) or **theirs** (they gave it to us), valid from/until
  so old and new work together while rotating. `ensure`, `set_status`, `generate_secret`,
  `add_secret`, `add_public_key`, `rotate`, `retire`, `credentials`, `listing` (fingerprints, no
  values), `reveal`, `reseal`. `Bridge(..., keyring=)` reads them; `secrets=` (config) still
  works and wins for a name it has.
- Standard Webhooks `v2a` (ed25519, the spec's asymmetric scheme): `verify` accepts it with a
  connection's public keys and endpoint id; `new_keypair`, `sign_v2a`, `public_key`;
  `headers(..., signing_key=, endpoint_id=)`. Checked against the spec's own example.
- `meydan.vault.Keyring`: secrets sealed at rest with AES-256-GCM, bound to their row, keys by id
  from `MEYDAN_KEYRING` (`k2:<base64>,k1:<base64>`, first is current) so the key can rotate.
- `python -m meydan.bridge secret | keypair | send URL --type --data`: make credentials, or be the
  other side and send a signed event (secrets from the environment).
### Changed
- `Bridge(...)`: `secrets` is optional (keyword); `verify` takes `Credentials` (or, as before, a
  list of secrets). docs/bridges.md: a "Words" section; meydan names only the technical link.
### Upgrading
- New tables `meydan_connections`, `meydan_connection_credentials`: generate a migration.
- To keep credentials in the database: add the `bridge` extra, set `MEYDAN_KEYRING`, pass
  `keyring=` to `Bridge`, and move secrets from config with `add_secret` (origin theirs) or
  `generate_secret`.

## [0.40.0] - 2026-11-25
### Added
- `meydan.bridge`: events from outside systems (a stakeholder's software, a payment provider, a booking
  tool). A connection is a name and a shared secret; requests are signed with
  [Standard Webhooks](https://www.standardwebhooks.com/) (`webhook-id`, `webhook-timestamp`,
  `webhook-signature`, `v1` HMAC-SHA256; several secrets while one is rotated; ±5 minutes).
  `Bridge.receive` verifies and stores an event once per (connection, event id), before the
  answer; `Bridge.run`/`drain` hand it to the app's handler for (connection, type) in its own
  transaction (a savepoint: a failing handler writes nothing), retry with backoff, then `dead`
  with the reason; `PermanentError` from a handler: dead at once; no handler: `ignored`;
  `Bridge.retry` puts one back. `meydan.bridge.web.routes`: `POST /{connection}/webhooks`.
  Helpers for senders and tests: `new_secret`, `sign`, `headers`, `verify`. docs/bridges.md.
- `expect.complete(...)`: an expectation completed by an outside event that carries its token
  (a reference in a link), once, with the button answer's checks; `NotCompletedError(reason)`.
### Upgrading
- New table `meydan_bridge_events`: generate a migration (`alembic revision --autogenerate`).
- From before 0.37: `meydan.operator.WINDOWS` is gone (0.37 did not say so). A channel's reply
  window is `runtime.rules.of(channel).reply_window` (`meydan.rules.DEFAULT` unless you set
  rules).

## [0.39.0] - 2026-11-22
### Added
- `Outbound.recall`: what the model reads of one of our messages in later turns, instead of its
  text (`transcript` reads it). The outbox keeps the real message for the person and for those
  allowed to see the conversation. `text_message(..., recall=...)`, `template_message(...)`.
- `Outbound.fallback`: sent in a message's place when it cannot go: refused for good (a dead
  letter) or reported `failed` later by a receipt. Queued once; dropped once the message is
  delivered (or sent, on a channel without delivery receipts). `text_message`, `image_message`,
  `template_message` take it.
- docs/privacy.md: what the model reads, the four ways personal data reaches it, what is planned.
  docs/failures.md: "When a message cannot go".

## [0.38.0] - 2026-11-22
### Added
- A template's image header: `Outbound.template_message(..., media=key)`, a file kept in
  meydan.media. The sender uploads it like an image message (once per sending number) and WhatsApp
  gets it as the header's `id`; Telegram sends a photo with the template's text as its caption.
- Numbered template parameters: `**{"1": "Ada"}` for a template approved with {{1}}, {{2}}…; they
  go in order, without names (WhatsApp refuses names for a numbered template).
### Changed
- The outbox uploads the file of any message that carries one (`message.media`), not only images.

## [0.37.0] - 2026-11-22
### Added
- `meydan.channels.instagram`: direct messages to an Instagram professional account (Instagram
  API). `parse_messages` (text, quick-reply taps as kind `button`,
  attachments, replies; echoes, deletions and reactions skipped), `parse_statuses` (reads),
  `Instagram.send` (text, choices as quick replies, a location or a contact card as text;
  templates and images refused with the reason; a text over 1000 bytes goes as several messages,
  cut where a line ends). Unified Login by default (graph.facebook.com, a system user token);
  `host=INSTAGRAM_LOGIN` for the older setup. Addresses `ig:` (our account) and `igsid:`
  (a person, scoped to our account). docs/instagram.md.
- `meydan.rules`: `INSTAGRAM` (24-hour window, 1000 bytes, reads) and `text_bytes_max` for a
  channel that counts bytes.
- `ReplyStyle.max_bytes`: an agent's bubbles fit a channel that counts bytes; the model is told
  to say it shorter instead of the send failing.
- `meydan.channels.meta.verify_signature`: Meta's webhook signature, shared by WhatsApp and
  Instagram.
### Changed
- `meydan.operator` reads a channel's reply window from `meydan.rules` (`runtime.rules`); its own
  copy (`WINDOWS`, WhatsApp only) is gone, so a person on the team can't write into a closed
  Instagram window either.
- `reply_payload` lives in `meydan.channels.base`: a tap's payload is channel-neutral, and
  `expect` and `auth` no longer import from the WhatsApp adapter.
### Upgrading
- Nothing to migrate. `meydan.channels.whatsapp.reply_payload` and `.verify_signature` still
  import from the old place.

## [0.36.0] - 2026-11-20
### Added
- Tool feedback, the result half of the agent-computer interface (docs/tools.md):
  `meydan.agent.tool(fn, context, prepare=)` builds a Pydantic AI `Tool` and sends the return
  schema with it when a field of the returned type has a description (describing a field is
  asking for the model to read it); `undescribed_tool(fn)` and `assert_tool_described(*fns)` for
  tests: a tool with no docstring, parameters with no description, returned fields (nested
  models and lists included) with no description.
### Upgrading
- Nothing to migrate. `as_tool` is unchanged; use `tool` where you built `Tool(as_tool(...))`.

## [0.35.0] - 2026-11-20
### Added
- Observe: each `Line` of a conversation has its `kind` and its parts, as the person saw them:
  `image` (media key, alt, caption), `location`, `choices` (question, options, buttons or list),
  `contact`. A message from the person says whether they `tapped` an option and which line of
  ours it answers (`reply_to`). The parts are hidden with the text.
- `GET /observe/media/{key}` (`query.media`): a file we sent, to a person who sees a conversation
  that it went out in; reads of others' conversations are audited (`read:media`).
  `observe.web.routes(..., media_store=)`.
### Upgrading
- Nothing to migrate.
### Docs
- README rewritten in plain technical English (ASD-STE100 style: short sentences, one term per
  concept, no filler). Every module and its doc in one table, recipes as links. The version is
  in one place: the badge reads the latest tag. The engagement table moved to docs/observe.md,
  the admin service recipe to docs/getting-started.md.
- docs/reply.md: wrap an attachment declared with `.of()` in a class of your own, so the model
  sees its name (`Venue`) in the schema rather than a generated one (`LocationClub`).

## [0.34.0] - 2026-11-20
### Added
- `Location.of(...)`, `Image.of(...)`, `Contact.of(...)`: an attachment declared from a table,
  "find the row with this key, use these columns", without writing `resolve`. Columns,
  `"{column}"` templates or callables for each part; `where` (fixed filters), `visible(ctx,
  row)`; `field` (what the model writes, default the key column's name). One SELECT through the
  Session in `ctx`. Columns of another table fail when the class is made. Needs SQLAlchemy. See
  docs/reply.md#attachments-what-goes-after-the-bubbles.
### Upgrading
- Nothing to migrate.

## [0.33.0] - 2026-11-20
### Added
- Reply attachments: what goes after the bubbles (a map pin, a photo, a contact card, choices).
  `attachments: Attachments[...]` in a `Reply` subclass; primitives `Location`, `Image`,
  `Contact` (implement `resolve`, return `Place` / `ImageRef` / `Person`) and `Choices` (ready;
  the model writes it). The model names a thing, code finds the value. Types are told apart by a
  fixed `type`; `when` is a type's description in the schema. `reply.outbound(ctx)` sends the
  bubbles, then each attachment in the model's order; one that resolves to nothing or fails is
  left out and logged. A reply may be attachments alone; `ReplyStyle.max_attachments` (3).
  See docs/reply.md#attachments-what-goes-after-the-bubbles.
- `assert_described(ReplyClass)` / `undescribed(...)`: for your tests, every field and
  attachment type the model would read without a description.
### Changed
- `Reply.bubbles` may be empty when the reply carries attachments (the schema's `minItems` is 0
  then). A reply class without attachments of its own is unchanged and shows none to the model.
- `ReplyStyle.problems(bubbles, attachments=None)`.
### Upgrading
- Nothing to migrate. Fields your `Reply` subclass added for a pin or a photo can become
  attachment types.

## [0.32.0] - 2026-11-20
### Added
- `meydan.media`: files we send, kept once by their SHA-256 (`media.put`, `media.load`). Where the
  bytes live is the app's choice: `MediaStore`; the default `PostgresStore` keeps them in
  `<prefix>media_blobs` (bytea). `Runtime(media_store=)`. See docs/media.md.
- `Outbound.image_message(key, caption=, alt=)`: an image, as a person sends a photo. The sender
  uploads it from the sending number on WhatsApp (`WhatsAppCloud.upload`) and reuses the media id
  for 29 days (`<prefix>media_uploads`); Telegram gets the file with `sendPhoto`. A key with no
  file is a dead letter.
### Upgrading
- New tables `<prefix>media`, `<prefix>media_blobs`, `<prefix>media_uploads` (autogenerate sees
  them).

## [0.31.0] - 2026-11-19
### Added
- `Outbound.contact_message(name, phone, org=, title=)`: a contact card. WhatsApp: a `contacts`
  message whose phone carries its WhatsApp id ("Message" and "Save contact"); Telegram:
  `sendContact`. `text` keeps the name, title, organisation and number for transcripts.
### Upgrading
- Nothing to migrate.

## [0.30.0] - 2026-11-19
### Added
- `Outbound.choices_message(question, [Choice(id, title, description)], label=)`: a question with
  1-10 options to tap (`meydan.channels.Choice`). WhatsApp shows up to 3 short options as reply
  buttons and anything else as a list (`whatsapp.choice_style` says which, or raises
  `ValueError` when neither fits; such a message fails to send without retries). Telegram: one
  inline button per row. `text` keeps the question and the options for transcripts. See
  docs/reply.md#choices.
### Changed
- `whatsapp.reply_payload` also returns a list row's id (`list_reply`), not only a button's.
- Telegram: a button press's text is the pressed button's label (it was empty).
### Upgrading
- Nothing to migrate. Outbox rows written before keep loading (`choices` defaults to none).

## [0.29.0] - 2026-11-19
### Added
- `notify.Kind` / `notify.Example`: the kinds an app sends, each with an example;
  `notify.test` sends a kind's example to one subscriber's own devices.
- Routes: `GET /notify/kinds`, `POST /notify/test` (`notify:subscribe`), and
  `POST /notify/notifications` (`notify:send`) for another system of the organisation sending
  through the same pipe (declared kinds only, links inside the app).

## [0.28.0] - 2026-11-19
### Added
- `meydan.notify` (extra `notify`): notifications to the team's own devices over Web Push.
  `subscribe`/`unsubscribe`, `enqueue` (in the transaction where the thing happened; one row per
  device), `Pusher` (sends what is due; 404/410 forgets the device, 429/5xx/no answer waits and
  gives up after `ATTEMPTS`, 400/413 never), `Keys` (VAPID). The message is the Declarative Web
  Push shape. See `docs/notify.md`.
- `meydan.notify.web`: `GET /key`, `POST`/`DELETE /subscriptions` (scope `notify:subscribe`;
  the device is the person's, or whom a service acts for).
- `Runtime(on_receive=[...])`: observers told of every new message in its transaction (before
  interceptors), and `Routed.new_actor`: the message is how we met them.
### Upgrading
- New tables `<prefix>notify_subscriptions`, `<prefix>notifications`, `<prefix>notify_deliveries`
  (autogenerate sees them).

## [0.27.0] - 2026-11-18
### Fixed
- `operator`: a takeover now stops the agent's reply even when it lands while the agent is
  thinking: the reply's own transaction checks again, and the agent's replies queued and not yet
  sent are cancelled (outbox status `cancelled`).
- `operator`: messages that arrive while a conversation is taken over are no longer marked
  processed. They wait; what a person answered (`send`) is marked answered; the agent answers
  what is still unanswered once the conversation is handed back or the takeover ends. Health
  does not count them as stuck work.
- `operator.send` never shortens a takeover (one until handed back stays so).
### Added
- `operator.takeover(..., replace=)`: someone else's takeover is not overwritten unless asked
  (`AlreadyTakenError`; HTTP 409, `{"replace": true}` to take it).
- Routes: `GET /operator/conversations/{id}` (one row however far down the list) and
  `GET /operator/summary` (counts only, nothing read, nothing audited). Takeover and handback are
  audited (`operator:takeover`, `operator:handback`, with `on_behalf_of`); unknown conversations
  are 404, not a database error.
- Observe: `Line.code`: a one-time code, marked as such (never inferred from a missing text).
### Upgrading
- The outbox status check gains `cancelled`: replace the constraint in a migration
  (`ALTER TABLE <prefix>outbox DROP CONSTRAINT ...; ADD CONSTRAINT ... CHECK (status IN (...,
  'cancelled'))`); autogenerate does not see check constraints.

## [0.26.0] - 2026-11-18
### Added
- `meydan.operator` (and `meydan.operator.web`, extra `api`): a person on the team stepping into a
  conversation the agent is having. **takeover** (a person takes it over: the agent does not
  answer while it holds; its messages are still received and recorded), **handback** (they give it
  back), writing as yourself (`author="staff"`, through the same queue; WhatsApp's 24-hour window
  enforced), and read marks per reader. Routes under `/operator`; writes need `operator:write`;
  an app's own words about its people come in through `labels`. **handoff** (the agent hands a
  conversation to a person) is named, not built yet. See docs/operator.md.
- `meydan.auth.issue_service_key` / `revoke_service_key`: another application (a dashboard, a back
  end) calls the API as its own actor with exactly the scopes given; the key is shown once, kept
  as a hash. A service may say whom it acts for (`X-On-Behalf-Of`, an identifier in the calling
  app): `Principal.on_behalf_of`, recorded in the audit trail, never authority.
- The outbox records who wrote each message when it is queued (`author`: agent, staff, system;
  `author_actor_id` for staff). A turn id is no proof the agent wrote it.
- Observe: a thread line has an id, its author, and for ours its delivery (receipts win over the
  send status). The conversation list carries the last message either way, its direction, author
  and text, redacted and audited as the thread is.
- Templates keep what they say: the channel's list brings the body and the buttons' labels
  (WhatsApp: `components`). A sent template shows its body filled with that message's parameters;
  an authentication template (a one-time code) never shows any words.
### Upgrading
- Tables change: run your migrations (e.g. `alembic revision --autogenerate`). New: `outbox.author`
  (default `'system'`) and `outbox.author_actor_id`, `audit.on_behalf_of`, `templates.body` and
  `templates.buttons`, and two tables `operator_reads`, `operator_holds`. Past agent replies can be
  marked as the agent's: `UPDATE <prefix>outbox SET author = 'agent' WHERE turn_id IN (SELECT id
  FROM <prefix>turns)`.
- Template bodies fill in at the next template sync (startup).

## [0.25.0] - 2026-11-17
### Added
- `meydan.llm`: `vertex-openai:<publisher>/<model>[@location]`, a partner or open model on Vertex
  AI (Model as a Service: DeepSeek, Llama, Qwen, ...) through its OpenAI-compatible endpoint, e.g.
  `vertex-openai:deepseek-ai/deepseek-v4.1-flash-maas@global`. Google's default credentials (a
  service account on a VM, `gcloud auth application-default login` on a laptop): no API key; the
  token is refreshed before it ends. Same retries, timeouts and concurrency limits as the others.
  Needs `MEYDAN_LLM_GCP_PROJECT`. Measured: the endpoint wants `<publisher>/<model>`; a bare model
  name is refused (400), and meydan refuses it first.
### Upgrading
- Nothing to migrate.

## [0.24.1] - 2026-11-16
### Fixed
- `templates/instance`: the webhook stores what Meta sent in a thread (`asyncio.to_thread`), not on
  the event loop it shares with the runtime's reader, worker and sender. A slow commit held every
  conversation's turn and reply for as long as it took.
### Upgrading
- An app made from the template: move the `with runtime.engine.begin()` block of `receive` into a
  function and `await asyncio.to_thread(...)` it, as the template now does.

## [0.24.0] - 2026-11-16
### Added
- `handler(agent, said=)`: what the model reads of the person's messages, from the turn. Left out,
  it is their text, or a note that there was none, as before. An app that gets contact cards, media
  or locations says what they are (`[Contact card: Ece]`) instead of the model reading a name as a
  message. A first step of [#18](https://github.com/pehlivan-ai/meydan/issues/18).
### Changed
- `handler`: `now(turn)`, `said(turn)` and `messages(output, turn)` run off the event loop, like the
  history. They are the app's and may read its database; one slow query no longer holds every
  other conversation's turn. `run(turn, history, prompt=)` takes a prompt made beforehand.
### Upgrading
- Nothing to migrate. A hook that relied on running on the event loop (calling `asyncio` from
  inside it) must not: they now run in a thread.

## [0.23.0] - 2026-11-16
### Changed
- Postgres 16 and 17 are supported, not only 18: the oldest supported version is now 16. Ids are made by meydan (`meydan.ids.uuid7`: a time-ordered
  UUID, version 7, RFC 9562, monotonic within a process by a 12-bit counter, section 6.2 method
  1) instead of Postgres 18's `uuidv7()`, so `ORDER BY id` stays insertion order, a reply's
  bubbles included. CI runs every test on Postgres 16, 17 and 18. The template's first migration
  no longer asks the database for ids.
### Upgrading
- Nothing to migrate. On Postgres 18 the old `uuidv7()` default stays and does no harm; SQLAlchemy
  supplies the id. An app that inserts into meydan's tables with raw SQL supplies the id itself
  (`meydan.ids.uuid7()`).

## [0.22.0] - 2026-11-16
### Added
- `meydan.reply`: a reply as bubbles, for agents that talk to people on a messaging app. `Reply` is
  structured output (a list of bubbles the model writes) with `outbound()` for the turn's handler;
  `ReplyStyle` is its shape (1 to 3 bubbles, 600 characters each, no markdown a messaging app shows
  as typed, no bubble that ends with ':'), checked where the reply is made, so a reply out of shape
  goes back to the model with what was wrong. Every default can be changed or switched off in a
  subclass, and `ReplyStyle.problems` works on plain strings ([docs/reply.md](docs/reply.md)). Two
  instances each had their own before.
- `meydan.runtime.route_by(resolve, handlers, default=)`: a handler per kind of sender, chosen in
  code. `resolve(turn)` gives a key (a role, the number written to); the turn goes to that key's
  handler. The key is resolved on every turn, a sender with no key goes to `default`, and a failed
  lookup fails the turn instead of answering as a stranger. What stands behind a key is the app's:
  one agent, several, or another `route_by` ([docs/routing.md](docs/routing.md)).
- `Telegram(..., markup="whatsapp")`: text written in WhatsApp's marks (`*bold*`, `_italic_`,
  `~strike~`, code) is sent as Telegram HTML, so one text reads the same on both channels. Left
  out, text goes as it is, as before. Checked against the Bot API's HTML rules: `<`, `>` and `&`
  escaped, marks that would cross are not formatted.
- A runtime takes only its own channel's turns and sends only its own channel's replies
  (`Runtime.serves`, from the channel's `name`; `WhatsAppCloud` and `Telegram` have one). Two
  runtimes on one database, such as a test with a fake channel next to a bot, never take each
  other's work. A channel without a name takes every turn, as before, and says so in the log.
- `templates/instance`: tests run on their own database (the app's name with `_test`, made and
  migrated by `tests/conftest.py`) and never read `.env`; CI runs them. Before, they used the
  database you develop on, and an app built from the template saw a test take a turn a real
  person was waiting for.
- `meydan.eval` (extra `eval`): scenarios for an agent on Pydantic Evals, which runs the cases,
  repeats them and reports. meydan adds what only it knows: `talk(handler, says)` runs a
  conversation through the same handler the runtime uses, and counts what the model was told it
  got wrong and sent again (`SentBack`, a score); evaluators for what an agent did (`Called`,
  `NoCalls`, `Says`, `DoesNotSay`, `MaxBubbles`); `summary(report)`, a line per case for a
  terminal. Two apps had each written their own runner.
- `meydan.agent.handler(agent, ...)`: an agent as a turn handler, what three apps each wrote
  around their agent: history from the transcript, a time limit, the turn's model calls recorded,
  the reply's bubbles sent. What is true only now (`clock`, `now(turn)`) goes at the end of the
  new message, never into the instructions, so the provider's prompt cache keeps the conversation
  (measured: 384 cached tokens on a turn's first call with the time in the instructions, about
  1,536 with it at the end); `NOW_NOTE` tells the model about that block. `handler.run(turn,
  history)` runs it without a database, for tests and evals.
- `meydan.agent.handler(agent, ...)`: an agent as a turn handler, what every app wrote around its
  agent (three apps, three copies): history from the transcript, a time limit, the turn's model
  calls recorded, the reply's bubbles sent. What is true only now (`clock`, `now(turn)`) goes at
  the end of the new message, never into the instructions, so the provider's prompt cache keeps
  the conversation (measured: 384 tokens read from the cache with the time in the instructions,
  about 1,536 with it at the end). `NOW_NOTE` tells the model what that block is.
  `handler.run(turn, history)` runs it without the database, for tests and evals.
- `meydan.rules`: what each channel allows (the WhatsApp reply window, message and button
  sizes, receipts, how "seen" leans), kept in one file, `meydan/rules.py`, each rule with its
  source. meydan's parts read it instead of keeping their own copies (the Telegram button limit,
  the engagement scale's seen bias). An app builds its own with only what it changes
  (`DEFAULT.but(whatsapp={...})`); there is no global setting.
- `Clock.calendar(days, start=)`: a calendar can start on another day than today, e.g. this
  week's Monday, for agents asked about days that already passed this week.
- Telegram: a second reader of the same bot (409) is waited out quietly after a restart, when
  the old long poll is still open, and named plainly in the log when it stays ("run one reader
  per bot"), instead of a stack trace every three seconds.
- `Runtime.health()`: whether due work is being done, read from the tables: the oldest message
  waiting for its turn and the oldest reply waiting to be sent (turns put off on purpose do not
  count), and replies given up in the last hour. The template's `/health` answers 503 when work
  is stuck, so the deploy's health check rolls back or restarts.
- `templates/instance`: a test leaves no work behind (a message waiting, a reply unsent), so the
  next test's app never answers it.
- `meydan.runtime.failures`: a failed turn is put off, tried again or given up by what failed
  (`transient`, `retry`, `human`, `permanent`), read from Pydantic AI's errors or an app's own
  (`TransientError`, `NeedsHumanError`, `PermanentError`). A provider that is down puts the turn
  off and it is answered when the provider is back, within the channel's reply window; credits
  that ran out stop it at once. `on_delay(turn, failure)` is called the first time a turn is put
  off; `on_failure(turn, failure)` when it is given up. Before, a turn was tried three times in a
  row and then dropped, whatever failed ([docs/failures.md](docs/failures.md)).
### Upgrading
- One new column: `meydan_inbox.retry_at` (nullable). Generate a migration
  (`alembic revision --autogenerate`).
- `on_failure`'s second argument is now a `Failure` (its `error` is the exception it used to be),
  and it is called only when a turn is given up. Only `retry` failures count toward
  `max_turn_attempts`.
- A channel's `name` decides which turns a runtime takes; a fake channel in your tests needs one.
- `templates/instance` has the migration for `retry_at`; its meydan pin moves to this release with it.

## [0.21.0] - 2026-11-13
### Added
- `meydan.booking`: the first **primitive**, an optional part an app takes only if it needs it (`meydan[booking]`).
  Appointments: tables for staff, services, shifts, bookings (no two bookings of one member overlap: an exclusion
  constraint, needs `btree_gist`), closures, a waiting list and a ledger of filled gaps; `core` holds, confirms,
  cancels, finds free gaps and offers a freed time to who waits; `policy.OfferPolicy` says when and how such an
  offer may be made (lead time, reply window, quiet hours, a cap per person). It knows nothing about channels or
  agents. Promoted from the lab after a pilot used it; tested against Postgres ([docs/booking.md](docs/booking.md)).
- README: "What you can build today": what an app can do with these parts, how to build one, and what a pilot had
  to build itself.
### Upgrading
- Nothing to migrate. An app that does not import `meydan.booking` is unaffected.

## [0.20.0] - 2026-11-13
### Added
- `meydan.agent`: the Pydantic AI adapter. `as_tool(fn, context)` exposes a `meydan.tools`
  tool to an agent (the person comes from the context, a `ToolError` becomes a result the
  model can explain); `history(lines)` turns a transcript into the messages a model is given
  before a turn. Both were in an instance until a second one needed them.
### Upgrading
- Nothing to migrate.

## [0.19.0] - 2026-11-13
### Added
- `templates/instance` is a small working app: webhook, app factory with the runtime loops,
  settings, Alembic with the first migration, `python -m app.migrate` as a rollback-safe deploy
  step, a `Dockerfile`, tests, and `try.py`, which talks to the app through a fake channel.
- Docs: `docs/getting-started.md` (for app developers and for work on meydan) and
  `docs/bring-up.md` (the accounts, keys and addresses an app on WhatsApp needs, in order).
### Changed
- `templates/instance`: the compose stack has a `migrate` step the app waits for;
  `deploy/deploy.sh` pins it to the release's image and skips it on rollback.
- meydan's own `pytest` collects only `tests/` (the template's tests run in a copy of it).
### Upgrading
- Nothing to migrate. An instance made from the older template keeps working; to take the
  migration step, copy `app/migrate.py`, the `migrate` service and the `pin` function.

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
