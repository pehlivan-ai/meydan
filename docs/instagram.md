# Instagram

`meydan.channels.instagram`, a channel next to WhatsApp and Telegram: direct messages to an
Instagram professional account, through the Instagram API with Unified Login (Meta's recommended
setup; the older Instagram Login setup works with `host=INSTAGRAM_LOGIN`). It follows the
same contract (`Inbound`, `Outbound`, `SendError`, a channel with `send` and `typing`) and
differs only where Instagram does. Reference:
[developers.facebook.com/documentation/instagram-platform](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api).

Request shapes are tested against a fake Graph API, from Meta's pages as read on 2026-11-22.
Against Meta itself (2026-11-22): a system user token reads the account and its conversations
on graph.facebook.com.

## What you need

- An Instagram **professional** account (business or creator) in a business portfolio. No
  Facebook Page.
- A Meta app with the "Instagram API" use case, and the permissions `instagram_basic` and
  `instagram_manage_messages` added to it.
- A token, as on WhatsApp: in Business Settings, a system user that has the app and the
  Instagram account assigned generates a token for the app with those two permissions, set to
  never expire. (A business you onboard grants one through a Facebook Login for Business
  configuration instead.) Check it with `GET /me/assigned_instagram_accounts`.
- Two subscriptions, as on WhatsApp: the app's webhook (callback URL, verify token, the
  `messages` field, and `messaging_seen` for reads) and the account's
  (`POST /<account id>/subscribed_apps?subscribed_fields=messages`). With only the first,
  nothing arrives.
- Standard access is enough for accounts that the app's own business manages. To serve other
  businesses' accounts, the app needs advanced access (App Review and business verification).

## Use

One runtime per channel, on the same tables: a WhatsApp runtime and an Instagram runtime side by
side each take only their own channel's turns.

```python
from meydan.channels.instagram import Instagram, parse_messages, parse_statuses, verify_signature
from meydan.reply import ReplyStyle
from meydan.rules import DEFAULT

channel = Instagram(settings.instagram_token)
runtime = Runtime(engine, runtime_tables, channel, reply)

# The agent's bubbles fit Instagram's limit, counted in bytes:
style = ReplyStyle(max_bytes=DEFAULT.of("instagram").text_bytes_max)


@router.post("/webhooks/instagram")
async def receive(request: Request) -> dict[str, str]:
    body = await request.body()
    if not verify_signature(secret, body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=401)
    payload = json.loads(body)

    def store() -> None:
        with engine.begin() as conn:
            runtime.receive(conn, parse_messages(payload), parse_statuses(payload))

    await asyncio.to_thread(store)
    return {"status": "ok"}
```

The verify handshake (`hub.mode`, `hub.verify_token`, `hub.challenge`) is the same as the
WhatsApp route in `templates/instance/app/webhook.py`.

## Same as WhatsApp, and where it differs

| | WhatsApp | Instagram |
|---|---|---|
| Our endpoint | `wa-pnid:<phone_number_id>` | `ig:<Instagram account id>` |
| A person | `tel:+…` or `wa-bsuid:…` | `igsid:<id>`, scoped to our account. No phone number, no name in the message |
| A message id | `wamid…` | Instagram's `mid`; `send` returns the `message_id` |
| Writing first | a template outside the 24-hour window | never: the person writes first. 24 hours from their last message, up to 7 days after a Click-to-Direct ad |
| Templates | approved by Meta, sent by name | none: a template `Outbound` is refused |
| Text | 4096 characters | 1000 bytes (UTF-8). Longer goes as several messages, cut where a line ends (`split_text`); `ReplyStyle.max_bytes` keeps an agent's bubbles under it |
| Choices | up to 3 buttons, else a list with descriptions | quick replies: up to 13, a title of 20 characters at most, plain text. Descriptions go in the question's text |
| A tap | kind `button` or `interactive` | kind `button`, payload in `button.payload`, so `reply_payload` and `Expectations` need no change |
| Location, contact card | native | text: the place with a maps link; the name and number |
| Image | uploaded per number | not yet: Instagram takes a public URL or its own upload. Refused, never dropped |
| Receipts | delivered, read | read (`messaging_seen`) |
| "typing…" | yes | not sent yet: Instagram's sender action names the person, the runtime passes a message id |
| Skipped | | our own messages coming back (`is_echo`), deletions, reactions, edits |

## Shared by Meta's channels

- `meydan.channels.meta.verify_signature`: the `X-Hub-Signature-256` check, the same on WhatsApp
  and Instagram.
- `meydan.channels.base.reply_payload`: what a tap carries, on every channel.
- `meydan.rules`: each channel's limits in one file. Instagram adds `text_bytes_max`.

## Not there yet

Each one waits for an app that needs it:

- private replies: one DM to a person who commented on a post, a reel or a live video;
- the human agent tag: a person on the team answers for 7 days after the person's last message;
- the 7-day window after a Click-to-Direct ad (the runtime uses the 24-hour rule);
- images, typing, story mentions and reactions as their own kinds.
