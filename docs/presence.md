# Presence: "seen" and "typing…"

Available since v0.3.1 (`meydan.runtime.presence`).

A person who reads a message marks it seen, and starts typing only after reading. A reply that
takes a while keeps showing them typing. The runtime does the same, in this order:

| Step | When (default) | Setting |
|---|---|---|
| Seen | ~1 s after a message arrives; during a burst, each new message again after ~1 s | `read_after_s` |
| Typing… | when the turn starts (the person stopped writing, `debounce_s`), never sooner than 0.8 s after seen | `typing`, `typing_after_read_s` |
| Typing refresh | every 20 s while the reply is still being made (WhatsApp drops the indicator after 25 s); only acts on slow turns | `typing_refresh_s` |
| Reply | bubbles paced like typing | `chars_per_second`, `max_bubble_gap_s` |

Show "typing…" only when you will reply: the runtime shows it only when a turn runs.

## Choosing, at the level you need

```python
RuntimeConfig()  # nothing: the defaults above ("human")
RuntimeConfig(presence="instant")  # seen and typing as soon as possible
RuntimeConfig(presence="quiet")  # no read receipts and no typing indicator
RuntimeConfig(presence=Presence(read_after_s=2))  # numbers
```

A policy function per moment (e.g. no "seen" at night) is planned; the named styles will be
written the same way.

## Measuring
The inbox records `received_at`, `read_at` and `typing_at` per message, and the outbox records
`created_at` and `sent_at` per reply message, all on the database clock: a turn's timeline is a
query. Tune the numbers from that, not from feel.

## Channels
A channel implements `typing(from, message_id)` (mark read + show typing) and, optionally,
`mark_read(from, message_id)`. Without `mark_read` there are no separate read receipts; typing
still works. WhatsApp implements both (`status: read`, with or without `typing_indicator`).
