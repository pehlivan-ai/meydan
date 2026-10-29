# Templates: knowing which ones you can send

Available since v0.9.0 (`meydan.runtime.templates`).

A WhatsApp template has a life of its own: it waits for review, gets approved or rejected, can be
paused for a few hours when people react badly to it, disabled, or moved to another category (a
utility template that reads like marketing becomes MARKETING, which costs more and needs opt-in).
meydan keeps each template's latest state and the sender asks it before sending one.

## What the sender does

| The template is | The message |
|---|---|
| APPROVED (or REINSTATED), or unknown to meydan | is sent |
| PENDING, PAUSED, IN_APPEAL, LIMIT_EXCEEDED | is **held**: out of the conversation's line, so replies behind it still go out |
| REJECTED, DISABLED, DELETED, ... | is given up (`dead`), with the template's status and reason |

When the template becomes sendable again, held messages go back in line, due at once. That is
the channel's own advice for paused templates: halt, and resume when they're active again.

## Where the state comes from

1. **Webhooks.** Subscribe your WhatsApp Business Account webhook to
   `message_template_status_update`, `template_category_update` and
   `message_template_quality_update`, then pass them on with the messages:

   ```python
   runtime.receive(
       conn, parse_messages(payload), parse_statuses(payload), parse_template_updates(payload)
   )
   ```

   Events are told apart by their content. An older status never overwrites a newer one
   (the channel retries and may deliver out of order).

2. **A sync at startup,** so a webhook missed while the app was down doesn't matter:

   ```python
   await templates.sync(engine, runtime_tables, whatsapp_cloud, account=waba_id)
   ```

   It reads `GET /<WABA_ID>/message_templates` (permission `whatsapp_business_management`).

## Reading it

`observe.query.templates(conn, tables, person)` (and `GET /observe/templates`) lists each template
with its status, category, an announced category change, quality, reason, and how many messages it
is holding. Needs the `operations` visibility item.

## Not yet

- Pacing: a send can come back as `held_for_quality_assessment`; meydan records it as sent, and the
  later delivery or failure receipt settles it.
