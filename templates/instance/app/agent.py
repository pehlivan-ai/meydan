"""What your app says. This is the part you write; the rest of the template is plumbing.

A turn is everything one person sent since the last reply, in the order they sent it.
`turn.actor_id` is who wrote, whichever address they used: key your data on it.
"""

from meydan.channels.base import Outbound
from meydan.runtime import Turn


async def reply(turn: Turn) -> list[Outbound]:
    return [Outbound.text_message(f"You wrote: {turn.text}")]
