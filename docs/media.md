# Media: files we send, and files people send us

Send an image the way a person sends a photo:

```python
from meydan import media
from meydan.channels import Outbound

with runtime.engine.begin() as conn:
    key = media.put(conn, tables, jpeg_bytes, "image/jpeg")  # SHA-256 of the bytes, hex

Outbound.image_message(key, caption=None, alt="Spring Cup poster")
```

- **One file, one key.** `media.put` keeps a file once, keyed by its SHA-256: the same bytes twice
  are one file. `<prefix>media` holds what meydan must know (type, size, which store has the
  bytes); the bytes are the store's.
- **Where the bytes live is the app's choice.** `MediaStore` (`name`, `put`, `get`, `delete`,
  each given the caller's connection). The default, `PostgresStore`, keeps them in
  `<prefix>media_blobs` (bytea): nothing else to run, and they are in the database's backups. Right
  for a few files; many or large ones belong in object storage, behind your own `MediaStore`,
  passed to the runtime (`Runtime(..., media_store=)`) and to `media.put`. Each file records its
  store, and is read only from that one.
- **The sender does the channel's part when the message leaves.** WhatsApp: the file is uploaded
  from the sending number (`WhatsAppCloud.upload`, Cloud API "Media"), and its media id is kept in
  `<prefix>media_uploads` and reused for that number until 29 days have passed (Meta keeps uploads
  30 days; an id works only from the number that uploaded it). Telegram: the file goes with the
  message (`sendPhoto`). Neither id nor bytes are ever stored on the outbox row.
- **Transcripts** show `[image: <alt>]` (and the caption), so an agent reading the conversation
  knows what it sent.
- A key with no file is a dead letter (no retry helps).
- WhatsApp takes JPEG, PNG and static WebP images up to 5 MB, 8-bit RGB(A), at most 4096 px on an
  edge: prepare the file before `media.put`.

## Files people send us

A person sends a photo; the agent should see it, the team should be able to open it, and it should
not be kept forever. meydan does the part every app needs, the same way on every channel:

```python
Runtime(engine, tables, channel, reply, RuntimeConfig(keep_received=timedelta(days=30)))
reply = handler(agent, engine=engine, tables=tables, images=True)  # a model that reads images
```

| Step | Who | What |
|---|---|---|
| Download | the channel (`download_media(message) -> (bytes, type)`) | WhatsApp: GET the media id, then its URL (5 minutes) with the token; ids from webhooks live 7 days. Telegram: `getFile`, the largest size |
| Keep | the worker, before the turn (`RuntimeConfig.fetch_media`, default `{"image"}`) | `media.receive`: a photo is made upright, its long edge at most `image_max_edge` (1568 px), JPEG, its metadata (camera, location) dropped (`prepare_image`, extra `media`: Pillow); kept like any file (`MediaStore`); the inbox row points at it (`media`). A file that can't be fetched leaves the message as it was: the turn still runs |
| Read | the agent (`handler(images=True, history_images=N)`) | the turn's photos go to the model with the text, as bytes; `history_images` puts the last N back into the history; the rest are `[image] caption` lines (`describe`) |
| Show | `meydan.observe` | `Line.image.media` for a received photo; `media()` serves it like the message it came in (own conversation, or someone who may read others' messages: audited `read:media`) |
| Forget | the runtime, hourly (`keep_received`) or `media.forget_received(conn, tables, older_than)` | the file goes, the message stays (`media` set to null). A file still used elsewhere (sent by us, or referenced by the app, e.g. a poster) stays |

Why this shape (the research behind it):

- **Bytes, not URLs or provider file ids.** A provider's file id saves re-sending, but it is tied to
  one provider; an agent that falls back across providers can't use it. A URL means a public
  endpoint for private photos. Bytes work with every provider and leak nothing.
- **Resize before keeping.** A model's cost follows pixels (Anthropic: 28 px patches, standard tier
  scaled to 1568 px; OpenAI: 32 px patches, "high" to 2048 px). A phone photo is 12 MP: kept at
  1568 px it costs about 1.5k input tokens wherever it goes.
- **History is a budget, not a rule.** Chat products keep every image in the context and resend it
  each turn. In a messaging conversation the photo matters most in its own turn; `history_images`
  decides how many come back, and the rest stay as a line of text.
- **Opt in.** Only a model that reads images should get them (OpenRouter lists `input_modalities`
  per model); `images` is off by default.
- **Keep it yourself.** Neither WhatsApp (7 days) nor Telegram promises to keep a file; the team
  may want to see it later, and retention is the app's decision.

Not here yet: other kinds (documents, audio, video: `fetch_media` takes their names once a channel
downloads them and an agent can use them), Instagram attachments, and cleaning up files we sent
that are no longer used.
