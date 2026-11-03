"""How far a message we sent got with the person: one ordinal scale for every channel.

    0 NONE       sent, nothing came back
    1 DELIVERED  it reached their device / mailbox
    2 SEEN       they saw it (WhatsApp: read; email: opened)
    3 ANSWERED   they answered it: explicitly (a button, a quoted reply, an email In-Reply-To)
                 or implicitly (they wrote before our next message)

Each level implies the ones below (an answer means it was seen), so the scale is ordinal and
usable as a label (e.g. feedback for an agent's replies). Nothing is stored: the level is derived
from the times meydan keeps, so any horizon can be asked for later ("within 24 h of sending").

A message the channel couldn't deliver (failed, dead letter) is not on the scale: that is the
system's failure, not the person's behaviour, and it would teach a model the wrong thing.

Channels see "seen" differently, so each says how its signal leans (`SEEN_BIAS`): WhatsApp's
read receipts are missing for people who turned them off ("under"); email opens fire when a
mail client prefetches images ("over").
"""

from datetime import datetime, timedelta
from enum import IntEnum
from typing import Literal

SeenBias = Literal["exact", "under", "over"]


class Engagement(IntEnum):
    NONE = 0
    DELIVERED = 1
    SEEN = 2
    ANSWERED = 3


# How each channel's "seen" signal leans; add a channel here when meydan gains one.
SEEN_BIAS: dict[str, SeenBias] = {
    "whatsapp": "under",  # read receipts can be turned off: SEEN is missing, never false
    "email": "over",  # open pixels fire on prefetch (e.g. privacy protection): SEEN can be false
}


def engagement(
    *,
    sent_at: datetime | None,
    delivered_at: datetime | None,
    seen_at: datetime | None,
    answered_at: datetime | None,
    within: timedelta | None = None,
) -> Engagement | None:
    """The level a message reached, counting only what happened `within` this long after it was
    sent (None: anything so far). None if it was never sent: then it isn't on the scale."""
    if sent_at is None:
        return None

    def counts(at: datetime | None) -> bool:
        return at is not None and (within is None or at - sent_at <= within)

    if counts(answered_at):
        return Engagement.ANSWERED
    if counts(seen_at):
        return Engagement.SEEN
    if counts(delivered_at):
        return Engagement.DELIVERED
    return Engagement.NONE
