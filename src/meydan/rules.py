"""What each channel allows: the platforms' facts, in this one file.

These are not an app's choices: WhatsApp decides how long a business may answer in free text,
Telegram decides how many bytes a button carries. meydan's parts read them from here and never
keep their own copy, so when a platform changes a rule, one line here changes.

Each rule says where it comes from. A rule nobody has checked for a channel is left out (None),
never guessed; None also means the channel has no such thing (Telegram has no reply window).

    from meydan.rules import DEFAULT

    DEFAULT.of("whatsapp").reply_window   # timedelta(hours=24)

There are no global settings: an app that knows better (a platform changed before meydan did)
builds its own `Rules` once, where it builds its parts, and passes it on:

    rules = DEFAULT.but(whatsapp=dict(reply_window=timedelta(hours=48)))
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Literal

SeenBias = Literal["under", "over"]


@dataclass(frozen=True)
class ChannelRules:
    """One channel's rules, as far as meydan needs them."""

    # How long after the person's last message we may write in free text; outside it only what
    # the channel allows instead (WhatsApp: an approved template). None: no window.
    reply_window: timedelta | None = None
    text_max: int | None = None  # characters in one text message
    text_bytes_max: int | None = None  # UTF-8 bytes in one text, where a channel counts bytes
    button_payload_max: int | None = None  # bytes a button carries back when pressed
    receipts: frozenset[str] = field(default_factory=frozenset)  # "delivered", "read"
    # How "seen" leans: "under" (can be missing, never false), "over" (can be false), None.
    seen_bias: SeenBias | None = None


WHATSAPP = ChannelRules(
    # The customer service window. Meta, WhatsApp Cloud API, "customer service windows".
    reply_window=timedelta(hours=24),
    # A text message body. Meta, Cloud API "text messages".
    text_max=4096,
    # Status webhooks. Meta, Cloud API "status messages".
    receipts=frozenset({"delivered", "read"}),
    # People can turn read receipts off: "seen" can be missing, never false.
    seen_bias="under",
)
TELEGRAM = ChannelRules(
    # No reply window: a bot may write to anyone who started it. Telegram Bot API.
    reply_window=None,
    # sendMessage `text`. Telegram Bot API.
    text_max=4096,
    # InlineKeyboardButton `callback_data`. Telegram Bot API.
    button_payload_max=64,
    # Telegram reports no delivery or read receipts to bots.
    receipts=frozenset(),
)
INSTAGRAM = ChannelRules(
    # The messaging window; up to 7 days after a Click-to-Direct ad, which an app sets itself.
    # Meta, Instagram API with Instagram Login, "Send messages".
    reply_window=timedelta(hours=24),
    # "Message text must be UTF-8 and be a 1000 bytes or less." Meta, "Send messages".
    text_bytes_max=1000,
    # `messaging_seen` webhooks; people can turn read receipts off. Meta, Instagram "Webhooks".
    receipts=frozenset({"read"}),
    seen_bias="under",
)
EMAIL = ChannelRules(
    # Delivery events from the provider; opens from a tracking pixel.
    receipts=frozenset({"delivered", "read"}),
    # Open pixels fire when a mail client prefetches images: "seen" can be false.
    seen_bias="over",
)


@dataclass(frozen=True)
class Rules:
    """Every channel's rules, by channel name."""

    channels: Mapping[str, ChannelRules]

    def of(self, channel: str) -> ChannelRules:
        """A channel's rules; a channel without a record has none."""
        return self.channels.get(channel) or ChannelRules()

    def but(self, **changes: Mapping[str, Any]) -> "Rules":
        """These rules with some values changed, by channel: `but(whatsapp={"text_max": 4000})`.
        A field that does not exist is an error, not a rule that silently isn't."""
        out = dict(self.channels)
        for name, values in changes.items():
            out[name] = replace(self.of(name), **values)
        return Rules(out)


DEFAULT = Rules(
    {"whatsapp": WHATSAPP, "telegram": TELEGRAM, "instagram": INSTAGRAM, "email": EMAIL}
)
