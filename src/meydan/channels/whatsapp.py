"""WhatsApp Cloud API adapter: verify and parse webhooks, send messages.

The webhook contract (see docs/architecture.md): signed with X-Hub-Signature-256, delivered at
least once and unordered, retried for days if we don't answer 200 within 5 seconds.
"""

import contextlib
import hashlib
import hmac
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from meydan.channels.base import (
    Address,
    EndpointUpdate,
    Inbound,
    Outbound,
    SendError,
    StatusUpdate,
    TemplateUpdate,
)

CHANNEL = "whatsapp"
GRAPH = "https://graph.facebook.com"

# Graph error codes worth retrying, with a minimum wait before the next attempt.
# Pair rate limit (too many messages to one user in a short time): Meta asks for 4^n backoff,
# so start from a few seconds; throughput / app / account limits and transient errors: short.
RETRYABLE_CODES = {
    131056: 4.0,  # pair rate limit hit
    130429: 1.0,  # throughput limit reached
    131000: 1.0,  # something went wrong (transient)
    131016: 2.0,  # service unavailable
    80007: 5.0,  # WhatsApp Business Account rate limit
    4: 5.0,  # application request limit
    2: 1.0,  # temporary service error
    1: 1.0,  # unknown/transient
}


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256 = 'sha256=' + HMAC-SHA256(app_secret, raw body)."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header.removeprefix("sha256="))


def _ts(value: str | int) -> datetime:
    return datetime.fromtimestamp(int(value), UTC)


def _text_of(message: dict[str, Any]) -> str:
    kind = message.get("type")
    if kind == "text":
        return message.get("text", {}).get("body", "")
    if kind == "button":  # quick-reply button on a template
        return message.get("button", {}).get("text", "")
    if kind == "interactive":
        inter = message.get("interactive", {})
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return reply.get("title", "")
    for media in ("image", "video", "document"):
        if kind == media:
            return message.get(media, {}).get("caption", "")
    if kind == "contacts":  # names only: numbers stay in the payload (`shared_contacts`)
        names = [c.get("name", {}).get("formatted_name", "") for c in message.get("contacts", [])]
        return ", ".join(n for n in names if n)
    return ""


@dataclass(frozen=True)
class SharedPhone:
    phone: str  # as written in the person's address book (not normalized)
    wa_id: str | None  # set when the number is on WhatsApp: digits, E.164 without "+"
    type: str | None  # CELL, HOME, WORK, ...

    @property
    def address(self) -> Address | None:
        """The number as a `tel` address when WhatsApp vouches for it (wa_id), else None: the
        app normalizes `phone` itself (country rules are the app's)."""
        return tel(self.wa_id) if self.wa_id else None


@dataclass(frozen=True)
class SharedContact:
    """A contact card someone sent us (attachment > Contact)."""

    name: str
    phones: tuple[SharedPhone, ...]


def shared_contacts(message: Inbound) -> list[SharedContact]:
    """The contact cards in a `contacts` message; [] for anything else."""
    if message.kind != "contacts":
        return []
    out = []
    for c in message.payload.get("contacts", []):
        name = c.get("name", {})
        phones = tuple(
            SharedPhone(phone=p.get("phone", ""), wa_id=p.get("wa_id") or None, type=p.get("type"))
            for p in c.get("phones", [])
            if p.get("phone") or p.get("wa_id")
        )
        out.append(SharedContact(name=name.get("formatted_name") or name.get("first_name", ""),
                                 phones=phones))  # fmt: skip
    return out


def reply_payload(message: Inbound) -> str | None:
    """What a pressed button carries: a template quick reply's payload, or an interactive
    button's id. None for anything else."""
    raw = message.payload
    if message.kind == "button":
        return raw.get("button", {}).get("payload")
    if message.kind == "interactive":
        reply = raw.get("interactive", {}).get("button_reply") or {}
        return reply.get("id")
    return None


def _values(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") == "messages":
                yield change.get("value", {})


# Address schemes this connector owns.
TEL = "tel"  # the person's phone number, E.164 with "+" (WhatsApp's wa_id / `from`, digits)
BSUID = "wa-bsuid"  # business-scoped user id, scoped to one Meta business portfolio
ENDPOINT = "wa-pnid"  # our side: the phone_number_id a message was written to


def is_bsuid(recipient: str) -> bool:
    """Business-scoped user id (e.g. "US.13491208655302741918") rather than a phone number."""
    return not recipient.lstrip("+").isdigit()


def tel(wa_id: str) -> Address:
    return Address(TEL, "+" + wa_id.lstrip("+"))


def parse_messages(payload: dict[str, Any], portfolio: str = "") -> list[Inbound]:
    """Every incoming message in a webhook payload, routed by our phone_number_id.

    The sender is the phone number (`from`) when WhatsApp shares it, else the business-scoped user
    id (`from_user_id`, BSUID): people with a WhatsApp username may write without sharing their
    number. When both are present in one message, the BSUID goes in `also`: WhatsApp itself
    asserts that they are the same person (`channel_cooccurrence`).

    `portfolio` scopes BSUIDs (they are unique per Meta business portfolio); "" for an instance
    with a single portfolio.
    """
    out = []
    for value in _values(payload):
        to = Address(ENDPOINT, value.get("metadata", {}).get("phone_number_id", ""))
        profiles: dict[str, dict[str, Any]] = {}
        for c in value.get("contacts", []):
            for key in (c.get("wa_id"), c.get("user_id")):
                if key:
                    profiles[key] = c.get("profile", {})
        for m in value.get("messages", []):
            phone, bsuid = m.get("from"), m.get("from_user_id")
            if not (phone or bsuid):
                continue  # nobody to answer; the raw payload is the app's to keep
            user = Address(BSUID, bsuid, portfolio) if bsuid else None
            sender = tel(phone) if phone else Address(BSUID, str(bsuid), portfolio)
            profile = profiles.get(phone or "") or profiles.get(bsuid or "") or {}
            out.append(
                Inbound(
                    channel=CHANNEL,
                    message_id=m["id"],
                    to=to,
                    sender=sender,
                    sent_at=_ts(m["timestamp"]),
                    kind=m.get("type", "unknown"),
                    text=_text_of(m),
                    payload=m,
                    sender_name=profile.get("name"),
                    also=(user,) if phone and user else (),
                    attrs={"username": profile["username"]} if profile.get("username") else {},
                    reply_to=(m.get("context") or {}).get("id"),
                )
            )
    return out


def parse_template_updates(payload: dict[str, Any]) -> list[TemplateUpdate]:
    """What WhatsApp says about our templates: review and pauses
    (message_template_status_update), category (template_category_update) and quality
    (message_template_quality_update). Told apart by their content, not the field name."""
    out = []
    for entry in payload.get("entry", []):
        account, at = str(entry.get("id", "")), _ts(entry.get("time") or 0)
        for change in entry.get("changes", []):
            v = change.get("value", {})
            if "message_template_name" not in v:
                continue
            template_id = v.get("message_template_id")
            common = {
                "channel": CHANNEL,
                "account": account,
                "name": v["message_template_name"],
                "language": v.get("message_template_language", ""),
                "at": at,
                "external_id": str(template_id) if template_id else None,
            }
            if "event" in v:  # review, pause, unpause, disable, delete
                info = v.get("rejection_info") or {}
                reason = info.get("reason") or v.get("reason") or v.get("title")
                out.append(
                    TemplateUpdate(
                        **common,
                        status=v["event"],
                        category=v.get("message_template_category"),
                        reason=None if reason in (None, "NONE") else reason,
                    )
                )
            elif "new_quality_score" in v:
                out.append(TemplateUpdate(**common, quality=v["new_quality_score"]))
            elif "new_category" in v:
                if v.get("correct_category"):  # announced: it changes in about 24 hours
                    out.append(
                        TemplateUpdate(
                            **common,
                            category=v["new_category"],
                            upcoming_category=v["correct_category"],
                        )
                    )
                else:
                    out.append(TemplateUpdate(**common, category=v["new_category"]))
    return out


def parse_statuses(payload: dict[str, Any], portfolio: str = "") -> list[StatusUpdate]:
    """Delivery receipts for messages we sent. A receipt for a message sent to a phone number
    names the recipient's BSUID too (`recipient_user_id`): both go in `recipient`."""
    out = []
    for value in _values(payload):
        for s in value.get("statuses", []):
            errors = s.get("errors") or []
            rid, user = s.get("recipient_id"), s.get("recipient_user_id")
            recipient: list[Address] = []
            if rid:
                recipient.append(Address(BSUID, rid, portfolio) if is_bsuid(rid) else tel(rid))
            if user and user != rid:
                recipient.append(Address(BSUID, user, portfolio))
            out.append(
                StatusUpdate(
                    channel=CHANNEL,
                    message_id=s["id"],
                    status=s["status"],
                    at=_ts(s["timestamp"]),
                    error=(
                        f"{errors[0].get('code')}: {errors[0].get('title')}" if errors else None
                    ),
                    recipient=tuple(recipient),
                )
            )
    return out


# The fields of a phone number we keep (GET /<WABA>/phone_numbers).
NUMBER_FIELDS = (
    "id,display_phone_number,verified_name,status,quality_rating,name_status,"
    "code_verification_status,platform_type,messaging_limit_tier,throughput,account_mode"
)
# Webhook fields that say a number changed: answered by syncing the account again, because they
# name the number by its display form only.
NUMBER_EVENTS = frozenset({"phone_number_quality_update", "phone_number_name_update"})


def number_checks(n: dict[str, Any]) -> list[dict[str, str]]:
    """What a phone number's state means for sending from it: checks (ok | warn | fail)."""
    out: list[dict[str, str]] = []

    def check(name: str, value: Any, ok: set[str], warn: set[str], hint: str = "") -> None:
        if value is None:
            return
        v = str(value)
        level = "ok" if v in ok else "warn" if v in warn else "fail"
        out.append({"name": name, "level": level, "detail": v if level == "ok" else v + hint})

    check(
        "status", n.get("status"), {"CONNECTED"},
        {"PENDING", "FLAGGED", "RATE_LIMITED", "RESTRICTED", "MIGRATED", "UNKNOWN"},
        ": not sending normally",
    )  # fmt: skip
    check("quality", n.get("quality_rating"), {"GREEN", "UNKNOWN", "NA"}, {"YELLOW"})
    check(
        "name", n.get("name_status"), {"APPROVED", "AVAILABLE_WITHOUT_REVIEW"},
        {"PENDING_REVIEW", "NONE", "EXPIRED"}, ": display name not approved",
    )  # fmt: skip
    check("verification", n.get("code_verification_status"), {"VERIFIED"},
          {"NOT_VERIFIED", "EXPIRED"}, ": request a code and verify")  # fmt: skip
    check("platform", n.get("platform_type"), {"CLOUD_API"}, {"NOT_APPLICABLE"},
          ": not registered on the Cloud API")  # fmt: skip
    return out


def number_update(account: str, n: dict[str, Any], at: datetime) -> EndpointUpdate:
    details = {
        k: n[k]
        for k in ("name_status", "code_verification_status", "platform_type", "account_mode")
        if n.get(k) is not None
    }
    if isinstance(n.get("throughput"), dict):
        details["throughput"] = n["throughput"].get("level")
    details["checks"] = number_checks(n)
    return EndpointUpdate(
        channel=CHANNEL, address=Address(ENDPOINT, str(n["id"])), at=at, account=account,
        display=n.get("display_phone_number"), name=n.get("verified_name"),
        status=n.get("status"), quality=n.get("quality_rating"),
        limit=n.get("messaging_limit_tier"), details=details,
    )  # fmt: skip


def number_events(payload: dict[str, Any]) -> set[str]:
    """Business accounts whose numbers changed (quality, limit, display name): sync them."""
    return {
        str(entry.get("id", ""))
        for entry in payload.get("entry", [])
        for change in entry.get("changes", [])
        if change.get("field") in NUMBER_EVENTS
    } - {""}


class GraphError(Exception):
    """A Graph API call that failed, with Meta's code and message (never the token)."""

    def __init__(self, status: int, code: Any, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code = status, code


class WhatsAppCloud:
    """Sends on behalf of any of our numbers: the caller passes `from_number` (phone_number_id),
    so replies go out from the number the person wrote to (reply affinity)."""

    def __init__(
        self,
        access_token: str,
        api_version: str = "v25.0",
        client: httpx.AsyncClient | None = None,
    ):
        self._token = access_token
        self._version = api_version
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5))

    async def _post(self, from_number: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{GRAPH}/{self._version}/{from_number}/messages"
        try:
            r = await self._client.post(
                url, json=body, headers={"Authorization": f"Bearer {self._token}"}
            )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise SendError(f"network: {type(e).__name__}", retryable=True) from e
        except httpx.HTTPError as e:  # e.g. empty token -> illegal header: config, not transient
            raise SendError(f"{type(e).__name__}: {e}", retryable=False) from e
        if r.status_code < 300:
            return r.json()
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        code = err.get("code")
        reason = f"{r.status_code} {code}: {err.get('message', r.text[:200])}"
        if code in RETRYABLE_CODES:
            raise SendError(reason, retryable=True, code=code, min_wait_s=RETRYABLE_CODES[code])
        if r.status_code == 429 or r.status_code >= 500:
            raise SendError(reason, retryable=True, code=code)
        raise SendError(reason, retryable=False, code=code)

    async def list_templates(self, account: str) -> list[TemplateUpdate]:
        """Every template of a business account as it stands now (for a sync at startup).
        Needs the whatsapp_business_management permission."""
        url = f"{GRAPH}/{self._version}/{account}/message_templates"
        params: dict[str, Any] | None = {
            "fields": "id,name,language,status,category,quality_score",
            "limit": 100,
        }
        now, out = datetime.now(UTC), []
        while url:
            r = await self._client.get(
                url, params=params, headers={"Authorization": f"Bearer {self._token}"}
            )
            r.raise_for_status()
            data = r.json()
            for tpl in data.get("data", []):
                quality = tpl.get("quality_score")
                out.append(
                    TemplateUpdate(
                        channel=CHANNEL,
                        account=account,
                        name=tpl["name"],
                        language=tpl.get("language", ""),
                        at=now,
                        external_id=str(tpl["id"]) if tpl.get("id") else None,
                        status=tpl.get("status"),
                        category=tpl.get("category"),
                        quality=quality.get("score") if isinstance(quality, dict) else quality,
                    )
                )
            url, params = data.get("paging", {}).get("next"), None  # `next` carries the query
        return out

    async def _graph(
        self, method: str, path: str, *, json: dict | None = None, params: dict | None = None
    ) -> dict[str, Any]:
        r = await self._client.request(
            method, f"{GRAPH}/{self._version}/{path}", json=json, params=params,
            headers={"Authorization": f"Bearer {self._token}"},
        )  # fmt: skip
        if r.status_code < 300:
            return r.json()
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        raise GraphError(r.status_code, err.get("code"), err.get("message", r.text[:200]))

    # Numbers: the business management side (permission whatsapp_business_management).
    # Order to bring a number in: add_number -> request_code -> verify_code -> register.

    async def list_numbers(self, account: str) -> list[EndpointUpdate]:
        """Every phone number of a business account as it stands now, with its checks."""
        now, out = datetime.now(UTC), []
        data = await self._graph(
            "GET", f"{account}/phone_numbers", params={"fields": NUMBER_FIELDS, "limit": 100}
        )
        while True:
            out += [number_update(account, n, now) for n in data.get("data", [])]
            after = data.get("paging", {}).get("cursors", {}).get("after")
            if not after or not data.get("paging", {}).get("next"):
                return out
            data = await self._graph(
                "GET", f"{account}/phone_numbers",
                params={"fields": NUMBER_FIELDS, "limit": 100, "after": after},
            )  # fmt: skip

    async def number(self, phone_number_id: str) -> dict[str, Any]:
        return await self._graph("GET", phone_number_id, params={"fields": NUMBER_FIELDS})

    async def add_number(
        self, account: str, country_code: str, phone_number: str, verified_name: str
    ) -> str:
        """Add a number to the business account (its display name goes to review). Returns the
        new phone_number_id. `phone_number` is the national part, digits only."""
        data = await self._graph(
            "POST", f"{account}/phone_numbers",
            json={"cc": country_code, "phone_number": phone_number,
                  "verified_name": verified_name},
        )  # fmt: skip
        return str(data["id"])

    async def request_code(
        self, phone_number_id: str, method: str = "SMS", language: str = "en_US"
    ) -> None:
        """Send the number a verification code by SMS or VOICE (landlines: VOICE)."""
        await self._graph(
            "POST", f"{phone_number_id}/request_code",
            json={"code_method": method, "language": language},
        )  # fmt: skip

    async def verify_code(self, phone_number_id: str, code: str) -> None:
        await self._graph("POST", f"{phone_number_id}/verify_code", json={"code": code})

    async def register(self, phone_number_id: str, pin: str) -> None:
        """Register the verified number on the Cloud API with its two-step verification PIN
        (six digits; choose one and keep it: re-registering needs it)."""
        await self._graph(
            "POST", f"{phone_number_id}/register",
            json={"messaging_product": "whatsapp", "pin": pin},
        )  # fmt: skip

    async def set_profile(self, phone_number_id: str, **fields: Any) -> None:
        """Business profile shown with the number: about, address, description, email,
        websites (list), vertical."""
        if not fields:
            raise ValueError("nothing to set")
        await self._graph(
            "POST", f"{phone_number_id}/whatsapp_business_profile",
            json={"messaging_product": "whatsapp", **fields},
        )  # fmt: skip

    async def profile(self, phone_number_id: str) -> dict[str, Any]:
        data = await self._graph(
            "GET", f"{phone_number_id}/whatsapp_business_profile",
            params={"fields": "about,address,description,email,websites,vertical"},
        )  # fmt: skip
        return (data.get("data") or [{}])[0]

    async def send(self, from_number: str, to: str, message: Outbound) -> str:
        """Send one message; returns the channel message id (wamid).

        `to` is a phone number (E.164 or wa_id digits) or a BSUID; a BSUID goes in `recipient`."""
        body: dict[str, Any] = {"messaging_product": "whatsapp"}
        body |= {"recipient": to} if is_bsuid(to) else {"to": to.lstrip("+")}
        if message.kind == "text":
            body |= {"type": "text", "text": {"body": message.text}}
        else:
            params = [
                {"type": "text", "parameter_name": k, "text": v} for k, v in message.params.items()
            ]
            template: dict[str, Any] = {
                "name": message.template,
                "language": {"code": message.language},
            }
            components: list[dict[str, Any]] = []
            if params:
                components.append({"type": "body", "parameters": params})
            if message.otp:  # authentication template: the code in the body and its button
                code = [{"type": "text", "text": message.otp}]
                components += [
                    {"type": "body", "parameters": code},
                    {"type": "button", "sub_type": "url", "index": "0", "parameters": code},
                ]
            for index, payload in enumerate(message.buttons):
                components.append(
                    {
                        "type": "button",
                        "sub_type": "quick_reply",
                        "index": str(index),
                        "parameters": [{"type": "payload", "payload": payload}],
                    }
                )
            if components:
                template["components"] = components
            body |= {"type": "template", "template": template}
        data = await self._post(from_number, body)
        return data["messages"][0]["id"]

    async def mark_read(self, from_number: str, message_id: str) -> None:
        """Mark a received message (and everything before it) read: "seen". Best effort."""
        with contextlib.suppress(Exception):
            await self._post(
                from_number,
                {"messaging_product": "whatsapp", "status": "read", "message_id": message_id},
            )

    async def typing(self, from_number: str, message_id: str) -> None:
        """Mark a received message read and show the typing indicator. Best effort."""
        with contextlib.suppress(Exception):
            await self._post(
                from_number,
                {
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": message_id,
                    "typing_indicator": {"type": "text"},
                },
            )

    async def aclose(self) -> None:
        await self._client.aclose()
