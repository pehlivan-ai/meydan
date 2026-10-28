"""Presence: when the other side sees "seen" and "typing…".

A person reads first, then starts typing; a reply that takes a while keeps showing them typing.
The defaults do that (docs/presence.md). Choose more only when you need to, at the level you need:

    RuntimeConfig()                                   # nothing: human-like defaults
    RuntimeConfig(presence="instant")                 # a named style: "human" | "instant" | "quiet"
    RuntimeConfig(presence=Presence(read_after_s=2))  # numbers

Times are measured on the database clock, like every queue time in the runtime.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Presence:
    # Read receipt ("seen") this long after a message arrives; None: no read receipts on their own.
    # During a burst, each new message is marked seen again after the same delay.
    read_after_s: float | None = 1.0
    # Show "typing…" when a turn starts (the person stopped writing, see debounce_s).
    typing: bool = True
    # ...but never sooner than this after "seen", so the order is visible: read, then type.
    typing_after_read_s: float = 0.8
    # Channels drop the indicator after a while (WhatsApp: 25 s); re-send it this often while the
    # reply is still being made. Only acts on slow turns. None: never.
    typing_refresh_s: float | None = 20.0

    @classmethod
    def human(cls) -> "Presence":
        """Read about a second after arrival, then type; the default."""
        return cls()

    @classmethod
    def instant(cls) -> "Presence":
        """Seen and typing as soon as possible (no human-like delays)."""
        return cls(read_after_s=0.0, typing_after_read_s=0.0)

    @classmethod
    def quiet(cls) -> "Presence":
        """No read receipts and no typing indicator (on WhatsApp, typing also marks read)."""
        return cls(read_after_s=None, typing=False, typing_refresh_s=None)

    @classmethod
    def of(cls, value: "Presence | str") -> "Presence":
        if isinstance(value, Presence):
            return value
        styles = {"human": cls.human, "instant": cls.instant, "quiet": cls.quiet}
        if value not in styles:
            raise ValueError(f"unknown presence style {value!r}; choose from {', '.join(styles)}")
        return styles[value]()
