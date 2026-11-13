# Bring-up list: what to get before a meydan app can talk

The minimum plumbing behind a meydan app on WhatsApp: the accounts, keys and addresses that
exist outside your code. Get them in this order; each row needs the ones above it. Add or
remove rows as the platform changes.

Last checked: 2026-11-11. Pages under `developers.facebook.com/documentation` were found that
day; dashboard links are the usual places and move often (marked *unchecked*).

## A shorter way in

To try an app with real phones before any of this exists, use Telegram: a bot token from
@BotFather is the whole list ([telegram.md](telegram.md)). The rows below are for WhatsApp.

## The first thing

A **Meta business portfolio**. The app, the WhatsApp account, the numbers, the templates and
the token all belong to it, so nothing else can start without one. One portfolio serves every
app and every number you run.

## The list

| # | Get | Where | You end up with | How |
|---|---|---|---|---|
| 1 | A Meta business portfolio | [business.facebook.com](https://business.facebook.com) (*unchecked*) | the owner of everything below | by hand, once |
| 2 | A Meta app with the WhatsApp use case ([steps](#creating-the-app)) | [Apps dashboard](https://developers.facebook.com/apps/) → Create App · [Get started](https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started) | **app id**, **app secret**; a test WhatsApp Business Account and test number once you open the use case and start using the API (row 3) | by hand, once |
| 3 | The WhatsApp Business Account id | in the app: Use cases → Customize (WhatsApp) → Start using the API creates the test account; then the API Setup page, or Business settings → Accounts → WhatsApp accounts | **account id** (WABA id) | by hand, read once |
| 4 | A system user with a token ([steps](#creating-the-system-user)) | [Business settings](https://business.facebook.com/latest/settings) → Users → System users · [Get started, permanent token](https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started) | **access token** with `whatsapp_business_messaging` and `whatsapp_business_management`; the app and the account assigned to the user | by hand, once |
| 5 | A public HTTPS address for the webhook | a tunnel while trying (cloudflared, ngrok); a VM with a domain when people depend on it (`templates/instance`) | **callback URL** ending in your webhook path | by hand |
| 6 | The webhook set on the app, subscribed to `messages` | the app's WhatsApp → Configuration · [Create a webhook endpoint](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/create-webhook-endpoint) · [Overrides per account or number](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/override/) | Meta calls your app; you chose the **verify token** | by hand today |
| 7 | The app subscribed to the account | [Manage webhooks](https://developers.facebook.com/documentation/business-messaging/whatsapp/solution-providers/manage-webhooks) (`subscribed_apps`) | webhooks for that account's numbers | by hand today |
| 8 | A number | [Register a business phone number](https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/registration/) · [endpoints.md](endpoints.md) | **phone number id** | `python -m meydan.cli numbers add / request-code / verify / register` |
| 9 | Templates, approved | WhatsApp Manager → Message templates · [Template API](https://developers.facebook.com/documentation/business-messaging/whatsapp/reference/whatsapp-business-account/message-template-api) · [templates.md](templates.md) | messages that reach people outside the 24-hour window | by hand today; meydan follows their state |
| 10 | A payment method on the account | WhatsApp Manager → Payment settings · [Pricing](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing) | paid templates can be sent | by hand, once |
| 11 | Business verification | Business settings → Security Center | more than two numbers; higher limits | by hand, once; takes days |
| 12 | Postgres 18 | local, or the instance template's compose stack | **database URL** | compose |
| 13 | A model key, if a model reads messages | [OpenRouter keys](https://openrouter.ai/settings/keys) (*unchecked*) · `meydan.llm` | **model key** | by hand, once |

Rows 1 to 4 are the handover: once you have the **app id**, the **app secret**, the **access
token** and the **account id**, everything after is done with those four values.

## What the app is told

The names our instances use in `.env`; an instance may choose others.

| Value | From row | Name |
|---|---|---|
| app secret | 2 | `META_APP_SECRET` |
| account id | 3 | `WHATSAPP_BUSINESS_ACCOUNT_ID` |
| access token | 4 | `WHATSAPP_ACCESS_TOKEN` |
| verify token | 6 | `WHATSAPP_VERIFY_TOKEN` |
| phone number id | 8 | per instance (one per number it answers) |
| database URL | 12 | `DATABASE_URL` |
| model key | 13 | `OPENROUTER_API_KEY` |

Secrets live in `.env` or a secret store, never in the repository or in a chat.

## Creating the app

Row 2, as the wizard showed it on 2026-11-11. Five steps, then **Create app**:

| Step | What to enter |
|---|---|
| App details | a name (`<framework or product>-dev` for a dev app) and a contact email you read |
| Use cases | **Connect with customers through WhatsApp** |
| Business | your business portfolio (an unverified one is accepted) |
| Requirements | nothing to do for this use case |
| Overview | check the four above, **Create app** |

Use cases can't be removed from an app later, so add only this one.

## Creating the system user

Row 4. A system user is the server's own account in the portfolio: its token keeps working
whoever logs in, and need not expire. The menu path was seen on 2026-11-11 (Turkish: Ayarlar →
Kullanıcılar → Sistem kullanıcıları); adding the user and assigning the app were walked through that day; the token step is Meta's, not yet walked through here.

| Step | What to do |
|---|---|
| Open | Business settings of the portfolio that owns the app → Users → System users |
| Add | a name that says whose it is (`<app>-server`), role Admin |
| Assign assets | the app ("Manage app", full access) and the WhatsApp account (full control). Only asset types the portfolio has are offered: no WhatsApp account in the dialog means row 3 isn't done yet |
| Generate token | for that app, never expiring, with `whatsapp_business_messaging` and `whatsapp_business_management` |
| Keep | the token is shown once: put it in `.env` as `WHATSAPP_ACCESS_TOKEN` |

- One system user per environment: the dev one is assigned only the dev app and test account.
- The WhatsApp account must exist first (row 3), or there is nothing to assign.

## Dev and prod

Anything that can answer real people exists twice, and the two never share a key.

| | Dev | Prod |
|---|---|---|
| Meta app (rows 2, 6) | its own app, callback on a tunnel | its own app, callback on the VM |
| WhatsApp account and number (rows 3, 7, 8) | the test account and test number that come with the dev app | the real account and numbers |
| Token (row 4) | a system user for dev, assigned only the dev app and test account | a system user for prod |
| Where secrets live | the developer's secret store (e.g. macOS Keychain, read by `mise`), or a gitignored `.env` | `.env` on the VM, root only |
| Database (row 12) | local | the VM's |
| Model key (row 13) | a dev key with its own spending limit | a prod key |

- Same variable names in both; only where the value lives differs. A prod token never sits on
  a laptop.
- Two apps subscribed to the **same** account both receive every message of its numbers, and
  both would answer. Keep the dev app on its own account, or use a per-number override (row 6).

## Notes

- **A number in use in the WhatsApp app** can't be registered as it is (row 8). Meta has a flow
  that keeps the app working beside the API
  ([onboarding business app users](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users));
  meydan doesn't support it yet.
- **The test number** that comes with row 2 only writes to recipients added to its allow-list.
- **Business info first** (row 1): creating a WhatsApp account is refused ("your business doesn't yet meet WhatsApp's policy requirements") until the portfolio's business info has its contact details. Fill them in under [Business settings → Business info](https://business.facebook.com/latest/settings/business_info), then try again (seen 2026-11-11).
- **One webhook per app**: pointing an app's callback somewhere else moves every number it
  serves. Use the overrides in row 6, or a second app, to try something beside a live instance.
- **By hand today** (rows 6, 7, 9) are done through Meta's own API as well; they are not in
  meydan's CLI yet.
