# Notifications (`meydan.notify`)

Tell the people who run the agent that something happened (someone new wrote, a booking needs a
look), on their own phones and computers. Not a channel: channels talk with the people the agent
serves; this talks to the team.

```
someone writes ──► Receiver ──► your observer (same transaction) ──► notify.enqueue
                                                                         │ one row per device
                                       Pusher (Web Push, VAPID) ◄────────┘
                                         │ 201 sent · 404/410 device forgotten
                                         ▼ 429/5xx later · 400/413 never
                                    the team's devices
```

Install the extra: `meydan[notify]` (pywebpush). The HTTP routes also need `api`.

## Keys

Web Push signs every request with a VAPID key pair (RFC 8292). Make one once and keep it:

```python
from meydan import notify

keys = notify.Keys.generate()
keys.private  # the secret half: keep it with your secrets
keys.public  # the page's applicationServerKey
```

Put them where your app reads secrets (`MEYDAN_NOTIFY_PRIVATE_KEY`, `MEYDAN_NOTIFY_PUBLIC_KEY` for
`Keys.from_env()`). New keys mean every device subscribes again. Without keys nothing is sent and
nothing is lost: deliveries wait.

## When something happens

Write the notification where it happened, in that transaction: it goes if and only if that
commits. For "someone new wrote", the runtime tells observers of every new message, and
`Routed.new_actor` says the message is how you met them:

```python
def new_person(conn, message, routed):
    if routed.new_actor:
        notify.enqueue(
            conn,
            tables,
            kind="new_person",
            title=f"{message.sender_name or 'Someone new'} wrote",
            body=message.text or "",
            url=f"/inbox/{routed.conversation_id}",
            tag=str(routed.conversation_id),
        )


runtime = Runtime(engine, tables, channel, agent, on_receive=[new_person])
```

`to=` names subscribers; without it every subscribed device gets it. `tag` folds notifications
about the same thing into one on the device.

## Sending

```python
pusher = notify.Pusher(engine, tables, keys=keys, subject="mailto:ops@example.com")
asyncio.create_task(pusher.run())  # or pusher.drain() from your own loop
```

What goes is the Declarative Web Push message (`{"web_push": 8030, "notification": {title, body,
navigate, tag}}`): browsers that know the format show it themselves, and a service worker reads
the same fields. It is encrypted end to end (RFC 8291): the push service carries it, it cannot
read it. Keep the words short (title 120, body 400 characters are kept) and remember they show on
a lock screen.

## Devices

A page subscribes with the public key (`PushManager.subscribe({userVisibleOnly: true,
applicationServerKey})`) and hands the result to your server:

```python
app.include_router(notify_routes(auth, tables, engine, keys=keys), prefix="/notify")
```

`GET /notify/key`, `POST /notify/subscriptions` (the body is `PushSubscription.toJSON()`),
`DELETE /notify/subscriptions` (`{"endpoint": ...}`), all with the `notify:subscribe` scope.
Whose device it is: the person's own session, or whom a service acts for (`X-On-Behalf-Of`). An
endpoint is one device: subscribing it again moves it to whoever subscribed last.

## Kinds, and trying them

Declare the kinds you send, each with an example, and give them to the routes:

```python
NEW_PERSON = notify.Kind(
    name="new_person",
    label="Someone new",
    when="Someone writes for the first time.",
    example=notify.Example(
        title="Anna (example)", body="Hi, a question about classes", url="/inbox"
    ),
)
app.include_router(
    notify_routes(auth, tables, engine, keys=keys, kinds=[NEW_PERSON]), prefix="/notify"
)
```

`GET /notify/kinds` lists them for a settings page, and `POST /notify/test` (`{"kind": ...}`) sends
that kind's example to the caller's own devices only: a person sees what a kind looks like on
their phone before the first real one happens. A new kind is one more `Kind`; its test comes with
it.

## One pipe for the organisation

Another system of the same organisation (the books, the bookings) sends through the same pipe
rather than keeping devices of its own: give it a service key with `notify:send` and it posts
`POST /notify/notifications` (`kind`, `title`, `body`, `url`, `tag`, optional `to`). Only declared
kinds go, and links stay inside the app (`url` starts with `/`). People turn notifications on once.
