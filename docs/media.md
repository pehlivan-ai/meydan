# Media: files we send

Send an image the way a person sends a photo:

```python
from meydan import media
from meydan.channels import Outbound

with runtime.engine.begin() as conn:
    key = media.put(conn, tables, jpeg_bytes, "image/jpeg")   # SHA-256 of the bytes, hex

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

Not here yet, on purpose: other kinds (documents, audio, video), media people send us, and
cleaning up files no longer used.
