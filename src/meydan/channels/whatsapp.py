"""WhatsApp Cloud API adapter: verify and parse webhooks, send messages.

The webhook contract (see docs/architecture.md): signed with X-Hub-Signature-256, delivered at
least once and unordered, retried for days if we don't answer 200 within 5 seconds.
"""

import contextlib
import hashlib
import hmac
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx

from meydan.channels.base import Inbound, Outbound, SendError, StatusUpdate

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
    return ""


def _values(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") == "messages":
                yield change.get("value", {})


def parse_messages(payload: dict[str, Any]) -> list[Inbound]:
    """Every incoming message in a webhook payload, routed by our phone_number_id."""
    out = []
    for value in _values(payload):
        to = value.get("metadata", {}).get("phone_number_id", "")
        names = {
            c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])
        }
        for m in value.get("messages", []):
            out.append(
                Inbound(
                    channel=CHANNEL,
                    message_id=m["id"],
                    to=to,
                    sender=m["from"],
                    sent_at=_ts(m["timestamp"]),
                    kind=m.get("type", "unknown"),
                    text=_text_of(m),
                    payload=m,
                    sender_name=names.get(m["from"]),
                )
            )
    return out


def parse_statuses(payload: dict[str, Any]) -> list[StatusUpdate]:
    """Delivery receipts for messages we sent."""
    out = []
    for value in _values(payload):
        for s in value.get("statuses", []):
            errors = s.get("errors") or []
            out.append(
                StatusUpdate(
                    channel=CHANNEL,
                    message_id=s["id"],
                    status=s["status"],
                    at=_ts(s["timestamp"]),
                    error=(
                        f"{errors[0].get('code')}: {errors[0].get('title')}" if errors else None
                    ),
                )
            )
    return out


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

    async def send(self, from_number: str, to: str, message: Outbound) -> str:
        """Send one message; returns the channel message id (wamid)."""
        body: dict[str, Any] = {"messaging_product": "whatsapp", "to": to}
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
            if params:
                template["components"] = [{"type": "body", "parameters": params}]
            body |= {"type": "template", "template": template}
        data = await self._post(from_number, body)
        return data["messages"][0]["id"]

    async def typing(self, from_number: str, message_id: str) -> None:
        """Mark a received message read and show the typing indicator. Best effort."""
        with contextlib.suppress(SendError):
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
