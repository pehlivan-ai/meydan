# Privacy: what the model reads

Available since v0.39.0 (`Outbound.recall`, `Outbound.fallback`).

What a person is sent and what the model reads are two things. The people a message is for, and
the people allowed to see the conversation (`meydan.observe`, behind `meydan.visibility`), get the
real message: it is the source of truth. The model reads only what it needs to go on talking.

Personal data reaches a model by four ways: what tools return, what people write, the history of
the conversation (our own messages included), and what the app puts in front of it. meydan closes
them one by one.

| Way in | What meydan gives | Since |
|---|---|---|
| Our own messages, in later turns | `recall`: the line the model reads instead of the text | v0.39.0 |
| What people write | `said(turn)` on the agent handler | v0.24.0 |
| What people send as files | photos reach the model only with `handler(images=True)`, resized and without metadata; kept `keep_received` long ([media.md](media.md#files-people-send-us)) | v0.45.0 |
| What tools return | `ToolReturn.metadata` (Pydantic AI) is never sent; field marks: planned | — |
| What the app puts in front | the app's own `now` / `in_front` | — |

## `recall`: two views of one message

```python
Outbound.text_message(
    timetable,  # the person reads this
    recall="[timetable: 23 Nov, 4 lessons]",  # the model reads this in later turns
)
```

The outbox row keeps `text` exactly as it was sent; `transcript` (what an agent's history is
built from) reads `recall` when there is one. An image already works this way: its transcript line
is `[image: <alt>]`.

## `fallback`: a message that cannot go

```python
Outbound.image_message(
    key, alt="timetable", fallback=Outbound.text_message(timetable, recall="[timetable: …]")
)
```

See [failures.md](failures.md#when-a-message-cannot-go). The fallback is a copy of what the
message says, so it is kept only while the message might still fail: it is dropped once the
message is delivered (or, on a channel that reports no deliveries, once it is sent).

## Planned

- Field marks: `Private[...]` on the models tools return, dropped (or counted) when a tool's
  result is serialized for the model.
- A gate at the model's door: a Pydantic AI capability whose `before_model_request` masks, in that
  request only, values marked private and patterns such as phone numbers, and logs loudly when it
  had to.
- `assert_private`: a test that runs an agent with canary values and fails if one reaches the
  model.
