"""When and how freed time is offered: the business's settings for asking.

Same shape as meydan's `Presence`: choose only as much as you need.

    OfferPolicy()                      # nothing: fair defaults
    OfferPolicy.of("fast")             # a named style: "fair" | "fast" | "gentle"
    OfferPolicy(lead_min=20)           # numbers

These are the instance's (business's) bounds. What a person agreed on (their waitlist window,
`auto_accept`) applies inside them; what the channel forbids (the 24 h window) applies on top.
"""

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta


@dataclass(frozen=True)
class OfferPolicy:
    # Never offer a slot that starts sooner than this: people need time to get there.
    lead_min: int = 45
    # How long an offer stays open before it moves on.
    reply_min: int = 15
    # People asked at once. 1 holds the time for them (fair, slower); more ask together and the
    # first yes books it (faster; the others get a "it's taken" note).
    fanout: int = 1
    # Local hours with no offers (start, end); an offer due then waits until it ends. None: any.
    quiet: tuple[int, int] | None = (21, 9)
    # Most offers one person gets in a day; None: no limit.
    per_person_day: int | None = 2

    @classmethod
    def fair(cls) -> "OfferPolicy":
        """One person at a time, in the order they asked; the default."""
        return cls()

    @classmethod
    def fast(cls) -> "OfferPolicy":
        """Three people at once, shorter replies, closer to the slot."""
        return cls(lead_min=20, reply_min=10, fanout=3, per_person_day=3)

    @classmethod
    def gentle(cls) -> "OfferPolicy":
        """Rarely and early: at most one offer a day, more time to answer."""
        return cls(lead_min=60, reply_min=30, per_person_day=1)

    @classmethod
    def of(cls, p: "OfferPolicy | str") -> "OfferPolicy":
        return p if isinstance(p, OfferPolicy) else getattr(cls, p)()

    def with_(self, **changes) -> "OfferPolicy":
        return replace(self, **{k: v for k, v in changes.items() if v is not None})

    @property
    def hold(self) -> bool:
        return self.fanout == 1

    def quiet_until(self, local: datetime) -> datetime | None:
        """End of the quiet hours `local` falls in (local time), or None if it's not quiet."""
        if self.quiet is None:
            return None
        start, end = self.quiet
        h = local.hour
        if not (h >= start or h < end) if start > end else not (start <= h < end):
            return None
        day = local.date() if h < end else local.date() + timedelta(days=1)
        return datetime.combine(day, time(end), local.tzinfo)
