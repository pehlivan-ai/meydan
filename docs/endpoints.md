# Endpoints: our own numbers and email addresses

Available since v0.14.0 (`meydan.runtime.endpoints`, `meydan.channels.email`, `python -m meydan.cli`).

People write to an app at a few addresses of its own: WhatsApp numbers, email addresses. Each has
a life the app doesn't control. A number is added, verified and registered. Its display name is
reviewed, it gets a quality rating and a messaging limit, and it can be flagged or banned. An
email address works only while its domain's DNS says so. meydan keeps the latest state of each
and reads it the same way for every channel: as **checks** (`ok`, `warn`, `fail`), with the
worst one as the endpoint's **health**.

Replies already go out from the number the person wrote to (reply affinity, `WhatsAppCloud`):
a second number needs no code change to receive and answer. What the app chooses is the number it
starts conversations from (templates, `ask`).

## Bringing a WhatsApp number in

The number must be able to receive one SMS or call, and must not be in use in the WhatsApp app.
Mobile numbers verify by SMS; landlines, toll-free and VoIP numbers by voice call (`--voice`).
Needs a system user token with `whatsapp_business_management` in `WHATSAPP_ACCESS_TOKEN`.

```
python -m meydan.cli numbers add --account WABA_ID --cc 90 --number 5551234567 --name "Brand"
python -m meydan.cli numbers request-code PHONE_NUMBER_ID            # or --voice
python -m meydan.cli numbers verify PHONE_NUMBER_ID 123456
python -m meydan.cli numbers register PHONE_NUMBER_ID                # asks for a 6-digit PIN
python -m meydan.cli numbers copy-profile OLD_ID PHONE_NUMBER_ID     # about, websites, email, ...
python -m meydan.cli numbers list --account WABA_ID                  # with its checks
```

- **The PIN** is the number's two-step verification PIN. Choose it and keep it with your
  secrets: registering the number again (another server, after a deregister) needs it.
- **The display name** goes to review when the number is added; until it's approved the name
  check says `warn`.
- **Webhooks** are subscribed per business account: a new number in an account the app is
  already subscribed to sends its messages to the same callback.
- **Unverified business portfolios** can register two numbers; business verification raises it.

## Checks

| Check | ok | warn | fail |
|---|---|---|---|
| `status` | CONNECTED | PENDING, FLAGGED, RATE_LIMITED, RESTRICTED, ... | BANNED, DISCONNECTED, ... |
| `quality` | GREEN (or not rated yet) | YELLOW | RED |
| `name` | APPROVED, AVAILABLE_WITHOUT_REVIEW | PENDING_REVIEW, NONE, EXPIRED | DECLINED, ... |
| `verification` | VERIFIED | NOT_VERIFIED, EXPIRED | |
| `platform` | CLOUD_API | NOT_APPLICABLE (not registered) | |
| `mx` (email) | MX records | | none |
| `spf` | one `v=spf1`, ending `-all` or `~all` | other endings | none, or several |
| `dkim:<selector>` | key published | no selector given | no key |
| `dmarc` | `p=quarantine` or `p=reject` | `p=none` (monitoring only) | none, invalid |
| `reports` | `rua=` set | no reports | |

## Keeping the state

```python
from meydan.channels.email import dns_health
from meydan.runtime import endpoints

await endpoints.sync(engine, tables, whatsapp_cloud, account=waba_id)  # at startup, periodically
with engine.begin() as conn:  # e.g. daily
    endpoints.record(conn, tables, dns_health("hello@example.com", selectors=["google"]))
```

Number webhooks (`phone_number_quality_update`, `phone_number_name_update`) name the number by
its display form only, so they are a signal to sync again: `whatsapp.number_events(payload)`
returns the business accounts to sync. A report older than the kept one is ignored; fields it
leaves out keep their value.

`observe.query.endpoints(conn, tables, person)` (and `GET /observe/endpoints`) lists them with
their checks. Needs the `operations` visibility item.

## Not yet

- Email as a channel (sending, reading, `In-Reply-To` answers, opens): the address and its DNS
  are tracked; messages aren't.
- Choosing the sending number by rule (country, load, health): the app passes it.
