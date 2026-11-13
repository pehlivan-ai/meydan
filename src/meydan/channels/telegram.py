"""Telegram Bot API adapter: bots are our endpoints, private chats are conversations.

    channel = Telegram([token, another_token], render=how_a_template_reads)
    runtime = Runtime(engine, tables, channel, reply)
    asyncio.create_task(poll(channel, bot, receive, stop))      # one per bot

No webhook and no public address: each bot is read by long polling (`getUpdates`, which does
not work while a webhook is set on the bot). A bot needs no review; it is made in a chat with
@BotFather. API reference: https://core.telegram.org/bots/api

How meydan's terms land here:
- our endpoint: `tg-bot:<bot id>` (the digits before ":" in the bot's token);
- a person: `tg:<user id>`; in a private chat the chat id is the user id, so that is `to`;
- a message id: `<bot>:<chat>:<message>`, unique across bots and chats; `send` returns the same
  shape, so a button press links to the message that carried it (`reply_to`);
- a button: Telegram has no templates. A template message is rendered by `render` into a text
  and one label per button; each button carries its payload as `callback_data` (64 bytes at
  most, enough for an `expect` token). A press arrives as kind "button", the way WhatsApp's
  quick replies do, so `Expectations` needs no change.

Details: docs/telegram.md.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from meydan.channels.base import Address, Inbound, Outbound, SendError

log = logging.getLogger(__name__)

CHANNEL = "telegram"
ENDPOINT = "tg-bot"  # our side: a bot, by its id
USER = "tg"  # a person, by their Telegram user id
API = "https://api.telegram.org"
POLL_S = 25  # how long Telegram holds a getUpdates call open
# Long enough for a connection that needs a few retransmits: on a lossy path a shorter limit
# gives up on attempts that would have got through.
CONNECT_S = 20
CALLBACK_DATA_MAX = 64  # bytes, Telegram's limit for what a button carries

Render = Callable[[Outbound], tuple[str, Sequence[str]]]  # template -> text, button labels


def bot_id(token: str) -> str:
    head, sep, _ = token.partition(":")
    if not (sep and head.isdigit()):
        raise ValueError("not a bot token (expected '<digits>:<secret>')")
    return head


def _plain(message: Outbound) -> tuple[str, Sequence[str]]:
    """A template nobody taught us to render: its name and parameters, buttons as options."""
    params = ", ".join(f"{k}: {v}" for k, v in message.params.items())
    return f"{message.template} ({params})", [b.rsplit(":", 1)[-1] for b in message.buttons]


class Telegram:
    def __init__(
        self,
        tokens: Iterable[str],
        render: Render | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self._tokens = {bot_id(t): t for t in tokens}
        self._render = render or _plain
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(POLL_S + 15, connect=CONNECT_S)
        )

    @property
    def bots(self) -> list[str]:
        return list(self._tokens)

    async def call(self, bot: str, method: str, **params: Any) -> Any:
        """One Bot API method. Raises SendError: retryable for rate limits and server errors."""
        token = self._tokens.get(bot)
        if token is None:
            raise SendError(f"no token for bot {bot}", retryable=False)
        try:
            r = await self._client.post(f"{API}/bot{token}/{method}", json=params)
        except httpx.HTTPError as e:
            # The token is in the URL: say what failed, never the request.
            raise SendError(f"{method}: {type(e).__name__}", retryable=True) from None
        data = r.json() if r.content else {}
        if r.status_code == 200 and data.get("ok"):
            return data["result"]
        wait = float((data.get("parameters") or {}).get("retry_after") or 0)
        raise SendError(
            f"{method}: {data.get('description') or r.status_code}",
            retryable=r.status_code == 429 or r.status_code >= 500,
            code=r.status_code,
            min_wait_s=wait,
        )

    async def send(self, from_number: str, to: str, message: Outbound) -> str:
        chat = int(to)
        if message.kind == "location":
            sent = await self.call(
                from_number, "sendLocation", chat_id=chat, latitude=message.lat,
                longitude=message.lng,
            )  # fmt: skip
        elif message.kind == "template":
            text, labels = self._render(message)
            if len(labels) != len(message.buttons):
                raise SendError(
                    f"template {message.template}: {len(message.buttons)} buttons, "
                    f"{len(labels)} labels",
                    retryable=False,
                )
            long = [b for b in message.buttons if len(b.encode()) > CALLBACK_DATA_MAX]
            if long:
                raise SendError(
                    f"template {message.template}: a button payload is over "
                    f"{CALLBACK_DATA_MAX} bytes",
                    retryable=False,
                )
            row = [
                {"text": label, "callback_data": payload}
                for label, payload in zip(labels, message.buttons, strict=True)
            ]
            markup = {"reply_markup": {"inline_keyboard": [row]}} if row else {}
            sent = await self.call(from_number, "sendMessage", chat_id=chat, text=text, **markup)
        else:
            sent = await self.call(from_number, "sendMessage", chat_id=chat, text=message.text)
        return f"{from_number}:{chat}:{sent['message_id']}"

    async def typing(self, from_number: str, message_id: str) -> None:
        _, chat, _ = (message_id.split(":") + ["", ""])[:3]
        if chat.lstrip("-").isdigit():  # a button press has no chat in its id
            await self.call(from_number, "sendChatAction", chat_id=int(chat), action="typing")

    async def updates(self, bot: str, offset: int | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": POLL_S,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            params["offset"] = offset
        return await self.call(bot, "getUpdates", **params)

    async def acknowledge(self, bot: str, update: dict[str, Any]) -> None:
        """Tell Telegram a button press arrived, so the button stops spinning."""
        press = update.get("callback_query")
        if press:
            await self.call(bot, "answerCallbackQuery", callback_query_id=press["id"])

    async def aclose(self) -> None:
        await self._client.aclose()


def _person(user: dict[str, Any]) -> tuple[Address, str | None, dict[str, Any]]:
    name = " ".join(n for n in (user.get("first_name"), user.get("last_name")) if n) or None
    attrs = {"username": user["username"]} if user.get("username") else {}
    return Address(USER, str(user["id"])), name, attrs


def parse_messages(bot: str, updates: Iterable[dict[str, Any]]) -> list[Inbound]:
    """Messages and button presses from private chats. Anything else is skipped."""
    out, to = [], Address(ENDPOINT, bot)
    for update in updates:
        if press := update.get("callback_query"):
            sender, name, attrs = _person(press["from"])
            asked = press.get("message") or {}
            chat = (asked.get("chat") or {}).get("id")
            mid = f"{bot}:cb:{press['id']}"
            out.append(
                Inbound(
                    CHANNEL, mid, to, sender, datetime.now(UTC), "button", "",
                    {"id": mid, "button": {"payload": press.get("data", ""), "text": ""}},
                    sender_name=name, attrs=attrs,
                    reply_to=f"{bot}:{chat}:{asked['message_id']}" if chat is not None else None,
                )
            )  # fmt: skip
        elif (m := update.get("message")) and m.get("chat", {}).get("type") == "private":
            if "from" not in m:
                continue
            sender, name, attrs = _person(m["from"])
            mid = f"{bot}:{m['chat']['id']}:{m['message_id']}"
            kind = "text" if "text" in m else "image" if "photo" in m else "unknown"
            out.append(
                Inbound(
                    CHANNEL, mid, to, sender, datetime.fromtimestamp(m["date"], UTC), kind,
                    m.get("text") or m.get("caption") or "", {"id": mid, **m},
                    sender_name=name, attrs=attrs,
                )
            )  # fmt: skip
    return out


async def poll(
    channel: Telegram,
    bot: str,
    receive: Callable[[list[Inbound]], None],
    stop: asyncio.Event,
) -> None:
    """Read one bot until `stop`: hand each batch to `receive` (which stores it), and only then
    move past it. A batch that fails to store is asked for again; the inbox drops repeats."""
    offset: int | None = None
    while not stop.is_set():
        try:
            batch = await channel.updates(bot, offset)
            if not batch:
                continue
            receive(parse_messages(bot, batch))
            offset = batch[-1]["update_id"] + 1
            for update in batch:
                try:
                    await channel.acknowledge(bot, update)
                except SendError:
                    log.warning("could not acknowledge a button press", exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reading bot %s failed; trying again", bot)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 3)
