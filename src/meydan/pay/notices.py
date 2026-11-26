"""Notices: a due's facts become messages to the person, once each, at the right moment.

Experimental (docs/pay.md). The app says which fact sends what; meydan does the rest. Patterns
from notification and workflow engines (Novu workflows: a dedupe key per trigger, `skip` per
step; Inngest functions: debounce with a maximum wait, sleep until a time, cancel when another
event comes), on meydan's facts (meydan.events):

- what: `Notice(on, name, card)`: when the fact `on` happens to a due ("due.opened",
  "due.paid"), `card(conn, due, reach)` makes the messages; it gets `reach.window_open`, so the
  app answers inside the channel's reply window with a free-form card and outside it with an
  approved template (the sender holds one still under review: docs/templates.md);
- whether: `skip(due)` decides when it is due to go (a pay card for a due that is no longer open
  is not sent: it was paid while the notice waited);
- when: `at(due)` holds it until a time (a reminder a day before the deadline; skipped then if the
  due was paid meanwhile: the "sleep until, cancel if paid" of workflow engines); and if a turn is
  about to run or running in their conversation (meydan.runtime.reach.busy) it waits and goes right
  after the agent's reply, at most `max_wait_s` after the fact (a stuck turn never holds it back);
- where: the person's last conversation (meydan.runtime.reach), from the endpoint they wrote to;
- once: each (due, notice) is one outbox message, ref `pay:<due>:<name>`; a fact delivered again
  sends nothing more.

Each notice is its own subscriber (a reminder waiting days never holds back the pay card):
`Runtime(..., subscribers=notices.subscribers())`.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import Connection, Engine, select

from meydan.channels.base import Outbound
from meydan.events import Event, Later, Subscriber
from meydan.pay.dues import Due, Dues, DueState
from meydan.runtime.reach import Reach, busy, reach

log = logging.getLogger(__name__)

Card = Callable[[Connection, Due, Reach], Sequence[Outbound] | None]


def _open_only(due: Due) -> bool:
    return due.state != DueState.OPEN


@dataclass(frozen=True)
class Notice:
    """When the fact `on` happens to a due, send what `card` makes (nothing: None or empty).
    `name` keys it once per due. `skip(due)`: not when this says so, as it stands when the notice
    is due. `at(due)`: not before this time (None: at once)."""

    on: str
    name: str
    card: Card
    skip: Callable[[Due], bool] | None = None
    at: Callable[[Due], datetime | None] | None = None


def opened(card: Card, name: str = "opened") -> Notice:
    """The pay card when a due opens; not if it was paid or closed before it went."""
    return Notice("due.opened", name, card, skip=_open_only)


def paid(card: Card, name: str = "paid") -> Notice:
    """The confirmation when a due is paid."""
    return Notice("due.paid", name, card, skip=lambda due: due.state != DueState.PAID)


def before_deadline(card: Card, ahead, name: str = "reminder") -> Notice:
    """One reminder `ahead` (a timedelta) of the deadline, if the due is still open then."""
    return Notice("due.opened", name, card, skip=_open_only, at=lambda due: due.deadline - ahead)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Notices:
    """The app's notices over its dues, as one subscriber of the runtime's facts."""

    def __init__(
        self,
        engine: Engine,
        runtime,
        dues: Dues,
        notices: Sequence[Notice],
        *,
        name: str = "pay.notices",
        wait_s: float = 2,
        max_wait_s: float = 120,
        now: Callable[[], datetime] = _utcnow,
    ):
        self.engine, self.runtime, self.dues = engine, runtime, dues
        self.notices, self.name = list(notices), name
        self.wait_s, self.max_wait_s, self.now = wait_s, max_wait_s, now

    def subscribers(self) -> list[Subscriber]:
        """One subscriber per notice, named `<name>.<notice>`: each waits on its own."""
        return [
            Subscriber(f"{self.name}.{n.name}", (n.on,), self._handler(n)) for n in self.notices
        ]

    def ref(self, due: Due, notice: Notice) -> str:
        return f"pay:{due.id}:{notice.name}"

    def _handler(self, notice: Notice):
        async def handle(event: Event) -> Later | None:
            if event.subject_id is None:
                return None
            with self.engine.begin() as conn:
                due = self.dues.get(conn, event.subject_id)
                return None if due is None else self._send(conn, due, notice, event)

        return handle

    def _send(self, conn: Connection, due: Due, notice: Notice, event: Event) -> Later | None:
        t = self.runtime.t
        ref = self.ref(due, notice)
        if conn.execute(select(t.outbox.c.id).where(t.outbox.c.ref == ref).limit(1)).first():
            return None  # sent before: a fact delivered again
        now = self.now()
        at = notice.at(due) if notice.at is not None else None
        if at is not None and now < at:
            return Later((at - now).total_seconds())
        if notice.skip is not None and notice.skip(due):
            return None
        where = reach(conn, t, self.runtime.rules, due.actor_id)
        if where is None:
            log.warning("due %s: %s not sent (they never wrote to us)", due.id, notice.name)
            return None
        waited = (now - max(event.at, at or event.at)).total_seconds()
        if busy(conn, t, where.conversation_id) and waited < self.max_wait_s:
            return Later(self.wait_s)  # right after the agent's reply
        messages = list(notice.card(conn, due, where) or ())
        if not messages:
            return None
        messages = [replace(m, ref=ref) if i == 0 else m for i, m in enumerate(messages)]
        self.runtime.enqueue(
            conn, channel=where.channel, conversation_id=where.conversation_id,
            from_number=where.to, to=where.sender, recipient_identity_id=where.identity_id,
            messages=messages,
        )  # fmt: skip
        return None


__all__ = ["Card", "Notice", "Notices", "before_deadline", "opened", "paid"]
