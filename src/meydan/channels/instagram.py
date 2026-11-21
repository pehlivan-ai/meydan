"""Instagram messaging adapter: DMs to a professional account (Instagram API with Unified Login).

    channel = Instagram(access_token)
    runtime = Runtime(engine, tables, channel, reply)
    # webhook: verify_signature(app_secret, body, header), then
    runtime.receive(conn, parse_messages(payload), parse_statuses(payload))

A professional (business or creator) account in a business portfolio; no Facebook Page. The token
is a system user's (the portfolio's own, or one a business grants through Facebook Login for
Business) with `instagram_basic` and `instagram_manage_messages`, on graph.facebook.com. The
account subscribes to the app (`POST /<account>/subscribed_apps`) and the app to the `messages`
webhook field. For an account the app's own business manages, standard access is enough; serving
other businesses' accounts needs advanced access (App Review). The older Instagram Login setup
(graph.instagram.com, an Instagram user token) works with `host=INSTAGRAM_LOGIN`.
Reference: developers.facebook.com/documentation/instagram-platform (messaging-api, webhooks).

How meydan's terms land here:
- our endpoint: `ig:<Instagram account id>` (the professional account written to);
- a person: `igsid:<Instagram-scoped id>`, scoped to our account: the same person has another id
  on another account, and no phone number or name comes with a message;
- a message id: Instagram's `mid`; `send` returns the `message_id` it answers with;
- the person writes first. We answer within 24 hours of their last message (7 days after a
  Click-to-Direct ad), and there are no templates to write outside that window;
- choices are quick replies (plain text, up to 13). A tap arrives as kind "button", the way a
  WhatsApp quick reply does, so `Expectations` and `reply_payload` need no change;
- a text over 1000 bytes goes as several messages, cut where a line ends;
- a location and a contact card have no Instagram form: they go as text (a maps link, the name
  and number). An image needs a public URL or Instagram's upload, which meydan.media doesn't
  provide yet: it is refused, never dropped silently.

Details: docs/instagram.md.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from meydan.channels.base import Address, Inbound, Outbound, SendError, StatusUpdate
from meydan.channels.meta import verify_signature
from meydan.rules import INSTAGRAM

__all__ = [
    "Instagram",
    "choice_problems",
    "parse_messages",
    "parse_statuses",
    "split_text",
    "verify_signature",  # X-Hub-Signature-256 with the Instagram app secret, as on WhatsApp
]

CHANNEL = "instagram"
ENDPOINT = "ig"  # our side: the professional account, by its Instagram id
USER = "igsid"  # a person, by their Instagram-scoped id (scoped to our account)
UNIFIED = "https://graph.facebook.com"  # Instagram API with Unified Login (Meta's recommended)
INSTAGRAM_LOGIN = "https://graph.instagram.com"  # Instagram API with Instagram Login

TEXT_BYTES_MAX: int = INSTAGRAM.text_bytes_max or 1000
# Quick replies. Meta, Instagram messaging "Quick replies": 13 at most, plain text, a title is
# cut after 20 characters (we refuse rather than let two titles end alike).
QUICK_REPLIES_MAX, QUICK_REPLY_TITLE_MAX = 13, 20

# Graph error codes worth retrying, with a minimum wait: rate limits and transient errors.
RETRYABLE_CODES = {
    1: 1.0,  # unknown / transient
    2: 1.0,  # temporary service error
    4: 5.0,  # application request limit
    17: 5.0,  # user request limit
    32: 5.0,  # page request limit
    613: 5.0,  # calls within one hour exceeded
}


def choice_problems(message: Outbound) -> list[str]:
    """What keeps these choices from going out as quick replies; [] when they fit. An app can
    check before it queues the message."""
    c = message.choices
    return [
        p
        for p in (
            f"over {QUICK_REPLIES_MAX} options" if len(c) > QUICK_REPLIES_MAX else "",
            f"a title over {QUICK_REPLY_TITLE_MAX} characters"
            if any(len(x.title) > QUICK_REPLY_TITLE_MAX for x in c)
            else "",
        )
        if p
    ]


def split_text(text: str, limit: int = TEXT_BYTES_MAX) -> list[str]:
    """`text` in parts of at most `limit` UTF-8 bytes: cut after a paragraph, else after a line,
    else after a word, else inside a word (never inside a character)."""
    parts: list[str] = []
    rest = text.strip()
    while len(rest.encode()) > limit:
        head = rest.encode()[:limit].decode(errors="ignore")  # a whole number of characters
        # The first separator that leaves a part of a fair size (a third of the limit or more).
        cut = next((i for sep in ("\n\n", "\n", " ") if (i := head.rfind(sep)) > len(head) // 3),
                   len(head))  # fmt: skip
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return [*parts, rest] if rest else parts


def _choices_text(message: Outbound) -> str:
    """The question, and each option's description under it: quick replies carry a title only."""
    described = [f"• {c.title}: {c.description}" for c in message.choices if c.description]
    return "\n\n".join(x for x in (message.body or "", "\n".join(described)) if x)


def _location_text(message: Outbound) -> str:
    link = f"https://www.google.com/maps/search/?api=1&query={message.lat},{message.lng}"
    return "\n".join(x for x in (message.place, message.address, link) if x)


def _contact_text(message: Outbound) -> str:
    parts = (message.contact_name, message.contact_title, message.contact_org,
             message.contact_phone)  # fmt: skip
    return "\n".join(x for x in parts if x)


class Instagram:
    """Sends from our professional account: the caller passes `from_number` (its Instagram id),
    so replies go out from the account the person wrote to."""

    name = CHANNEL

    def __init__(
        self,
        access_token: str,
        api_version: str = "v27.0",
        client: httpx.AsyncClient | None = None,
        host: str = UNIFIED,
    ):
        self._token = access_token
        self._version = api_version
        self._host = host
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5))

    async def send(self, from_number: str, to: str, message: Outbound) -> str:
        if message.kind == "template":
            raise SendError(
                f"template {message.template}: Instagram has no templates; write within 24 hours "
                "of the person's last message",
                retryable=False,
            )
        if message.kind == "image":
            raise SendError(
                "image: Instagram takes a public URL or its own upload; not supported yet",
                retryable=False,
            )
        if message.kind == "choices":
            if problems := choice_problems(message):
                raise SendError(
                    "choices don't fit Instagram: " + ", ".join(problems), retryable=False
                )
            body: dict[str, Any] = {
                "text": _choices_text(message),
                "quick_replies": [
                    {"content_type": "text", "title": c.title, "payload": c.id}
                    for c in message.choices
                ],
            }
        elif message.kind == "location":
            body = {"text": _location_text(message)}
        elif message.kind == "contact":
            body = {"text": _contact_text(message)}
        else:
            body = {"text": message.text}
        if not body["text"].strip():
            raise SendError("empty text", retryable=False)
        # Over Instagram's limit, the text goes as several messages, cut where a line ends; the
        # quick replies ride on the last one. A part that fails after others went is not tried
        # again (that would send those twice): it stops with how far it got.
        *before, last = split_text(body["text"])
        for n, part in enumerate(before):
            try:
                await self._post(from_number, {"recipient": {"id": to}, "message": {"text": part}})
            except SendError as e:
                if n == 0:
                    raise
                raise SendError(
                    f"sent {n} of {len(before) + 1} parts, then: {e.reason}", retryable=False,
                    code=e.code,
                ) from e  # fmt: skip
        try:
            sent = await self._post(
                from_number, {"recipient": {"id": to}, "message": {**body, "text": last}}
            )
        except SendError as e:
            if not before:
                raise
            raise SendError(
                f"sent {len(before)} of {len(before) + 1} parts, then: {e.reason}",
                retryable=False, code=e.code,
            ) from e  # fmt: skip
        return str(sent["message_id"])

    async def typing(self, from_number: str, message_id: str) -> None:
        """Nothing yet: Instagram's sender actions name the person, and the runtime passes the
        message it answers."""

    async def _post(self, account: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._host}/{self._version}/{quote(account, safe='')}/messages"
        try:
            r = await self._client.post(
                url, json=body, headers={"Authorization": f"Bearer {self._token}"}
            )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise SendError(f"network: {type(e).__name__}", retryable=True) from e
        except httpx.HTTPError as e:  # e.g. an empty token: config, not transient
            raise SendError(f"{type(e).__name__}: {e}", retryable=False) from e
        if r.status_code < 300:
            return r.json()
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        code, sub = err.get("code"), err.get("error_subcode")
        reason = f"{r.status_code} {code}/{sub}: {err.get('message', r.text[:200])}"
        if code in RETRYABLE_CODES:
            raise SendError(reason, retryable=True, code=code, min_wait_s=RETRYABLE_CODES[code])
        if r.status_code == 429 or r.status_code >= 500:
            raise SendError(reason, retryable=True, code=code)
        raise SendError(reason, retryable=False, code=code)

    async def aclose(self) -> None:
        await self._client.aclose()


def _events(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if payload.get("object") != "instagram":
        return
    for entry in payload.get("entry", []):
        yield from entry.get("messaging", [])


def _ts(ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, UTC)


def parse_messages(payload: dict[str, Any]) -> list[Inbound]:
    """Every DM someone sent us in a webhook payload. Our own messages coming back (`is_echo`),
    deleted ones and reactions, reads and edits are skipped."""
    out = []
    for e in _events(payload):
        m = e.get("message")
        if not m or m.get("is_echo") or m.get("is_deleted") or m.get("is_self"):
            continue
        ours, theirs = (e.get("recipient") or {}).get("id"), (e.get("sender") or {}).get("id")
        if not (ours and theirs and m.get("mid")):
            continue
        raw = {"id": m["mid"], **m}
        if qr := m.get("quick_reply"):
            kind, text = "button", m.get("text", "")
            raw["button"] = {"payload": qr.get("payload", ""), "text": text}
        elif m.get("attachments"):
            kind, text = str(m["attachments"][0].get("type") or "unknown"), m.get("text", "")
        elif m.get("is_unsupported"):
            kind, text = "unsupported", ""
        else:
            kind, text = "text", m.get("text", "")
        out.append(
            Inbound(
                channel=CHANNEL,
                message_id=m["mid"],
                to=Address(ENDPOINT, str(ours)),
                sender=Address(USER, str(theirs), str(ours)),
                sent_at=_ts(e.get("timestamp") or 0),
                kind=kind,
                text=text,
                payload=raw,
                reply_to=(m.get("reply_to") or {}).get("mid"),
            )
        )
    return out


def parse_statuses(payload: dict[str, Any]) -> list[StatusUpdate]:
    """Reads: the person saw a message of ours (`messaging_seen`)."""
    out = []
    for e in _events(payload):
        mid = (e.get("read") or {}).get("mid")
        if mid:
            reader = (e.get("sender") or {}).get("id")
            ours = (e.get("recipient") or {}).get("id")
            recipient = (Address(USER, str(reader), str(ours or "")),) if reader else ()
            out.append(StatusUpdate(CHANNEL, mid, "read", _ts(e.get("timestamp") or 0),
                                    recipient=recipient))  # fmt: skip
    return out
