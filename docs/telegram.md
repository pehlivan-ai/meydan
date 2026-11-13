# Telegram

`meydan.channels.telegram`, a second channel next to WhatsApp. It follows the same contract
(`Inbound`, `Outbound`, `SendError`, a channel with `send` and `typing`) and differs only where
Telegram does. API reference: [core.telegram.org/bots/api](https://core.telegram.org/bots/api).

Request shapes are tested against a fake Bot API. Against Telegram itself (2026-11-11): reading
a bot by long polling, sending text, a question with buttons and its answer by a press all work.

## What you need

One bot per endpoint, made in a chat with [@BotFather](https://t.me/BotFather) (`/newbot`): it
gives a token `<bot id>:<secret>`. No app, no review, no public address.

## Use

```python
from meydan.channels.telegram import Telegram, poll

channel = Telegram([token], render=render)  # several tokens: several bots
runtime = Runtime(engine, runtime_tables, channel, reply)


def receive(messages):  # store, as a webhook would
    with engine.begin() as conn:
        runtime.receive(conn, messages, [])


for bot in channel.bots:  # beside runtime.run(stop)
    asyncio.create_task(poll(channel, bot, receive, stop))
```

`poll` reads one bot by long polling (`getUpdates`). It moves past a batch only after
`receive` stored it; a batch asked for again is dropped by the inbox, as with WhatsApp's
repeated webhooks. Long polling does not work while a webhook is set on the bot.

## Same as WhatsApp, and where it differs

| | WhatsApp | Telegram |
|---|---|---|
| Our endpoint | `wa-pnid:<phone_number_id>` | `tg-bot:<bot id>` (the digits before `:` in the token) |
| A person | `tel:+…` or `wa-bsuid:…` | `tg:<user id>`; no phone number is shared |
| `to` when sending | the person's number | the chat id (in a private chat, the user id) |
| A message id | `wamid…` | `<bot>:<chat>:<message>`; `send` returns the same shape |
| How messages arrive | webhook, signed | long polling; nothing to verify, nothing to expose |
| Writing first | a template outside the 24-hour window | free, once the person has started the bot |
| Templates | approved by Meta, sent by name | none: `render(message)` turns a template `Outbound` into text and button labels |
| Buttons | template quick replies | inline buttons carrying the same payloads (`callback_data`, 64 bytes at most) |
| A button press | kind `button`, payload in `button.payload` | the same shape, so `Expectations` and `reply_payload` need no change |
| Receipts (delivered, read) | yes | none: the engagement scale stops at "sent" |
| "Seen" | read receipt | none; "typing…" works |
| Groups | not handled | skipped (private chats only) |

## Templates without templates

Telegram has no approved templates, so the app says how each of its templates reads there:

```python
def render(message: Outbound) -> tuple[str, Sequence[str]]:
    text = "{name}, you are invited.".format(**message.params)
    return text, ("Accept", "Decline")  # one label per button, in order
```

Keeping the wording next to the template's name means one source for both channels: what
Telegram shows is what is submitted to Meta as the template's body. Without `render`, a
template goes out as its name and parameters, with the option names as labels.

## Errors

A failed call raises `SendError`: retryable for rate limits (with the wait Telegram names,
`retry_after`) and server errors, permanent otherwise (a person who blocked the bot, a chat
that doesn't exist). The token is in the request URL, so errors name the method and never the
request.

## Not here (yet)

- Webhook delivery (`setWebhook` with a secret token) for deployments that prefer it.
- Media in either direction; a location goes out as a pin without its place name.
- Removing a question's buttons once it is answered (the press is acknowledged, the buttons stay).
