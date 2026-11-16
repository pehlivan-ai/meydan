"""A failed turn is put off, tried a few more times or given up, by what failed: a provider that
is down for an hour is not a poison message, and credits that ran out are not worth retrying.
Real Postgres 18 for the runtime; the clock is moved by moving `retry_at`."""

from datetime import timedelta

import pytest
from conftest import T0, FakeChannel, msg, receive, runtime
from pydantic_ai import exceptions as ai
from sqlalchemy import select, update

from meydan.channels.base import Outbound
from meydan.rules import DEFAULT
from meydan.runtime.failures import NeedsHumanError, PermanentError, TransientError, classify

M = "some-model"


class PaymentsDownError(TransientError):
    pass


class AccountClosedError(NeedsHumanError):
    kind = "account_closed"


@pytest.mark.parametrize(
    ("error", "kind", "group"),
    [
        (ai.ModelQuotaExceededError(M, "no credits"), "quota_exhausted", "human"),
        (ai.ModelRateLimitError(M, "slow down", retry_after=7), "rate_limited", "transient"),
        (ai.ModelOverloadedError(M, "busy"), "model_unavailable", "transient"),
        (ai.ModelContextWindowExceededError(M, "too long"), "context_too_long", "permanent"),
        (ai.ContentFilterError("refused"), "model_refused", "permanent"),
        (ai.UnexpectedModelBehavior("out of shape"), "model_misbehaved", "retry"),
        (TimeoutError(), "timeout", "transient"),
        (RuntimeError("a bug"), "error", "retry"),
        (PaymentsDownError(), "payments_down_error", "transient"),
        (AccountClosedError(), "account_closed", "human"),
        (PermanentError(), "permanent_error", "permanent"),
    ],
)
def test_a_failure_is_named_and_given_its_group(error, kind, group):
    assert classify(error)[:2] == (kind, group)


def test_a_wait_the_provider_asks_for_is_kept():
    assert classify(ai.ModelRateLimitError(M, "slow down", retry_after=7))[2] == 7


def test_when_every_model_of_a_chain_failed_the_most_hopeful_one_decides():
    group = ai.FallbackExceptionGroup(
        "all failed",
        [ai.ModelQuotaExceededError(M, "no credits"), ai.ModelOverloadedError(M, "busy")],
    )
    assert classify(group)[:2] == ("model_unavailable", "transient")


class Flaky:
    """A handler that fails with `errors`, one per try, then answers."""

    def __init__(self, *errors: BaseException):
        self.errors, self.tries = list(errors), 0

    async def __call__(self, turn):
        self.tries += 1
        if self.errors:
            raise self.errors.pop(0)
        return [Outbound.text_message("cevap")]


def said(turn, failure):
    return [Outbound.text_message(f"{'gave up' if failure.final else 'late'}: {failure.kind}")]


def inbox(rt):
    with rt.engine.connect() as c:
        return c.execute(select(rt.t.inbox)).one()


def due_now(rt):
    with rt.engine.begin() as c:
        c.execute(update(rt.t.inbox).values(retry_at=T0))  # as if the wait had passed


async def sent(rt, ch):
    while await rt.send_once():
        pass
    return [body for _, _, body in ch.sent]


async def test_a_provider_that_is_down_puts_the_turn_off_and_answers_when_it_is_back(db):
    ch = FakeChannel()
    handler = Flaky(ai.ModelOverloadedError(M, "busy"), ai.ModelOverloadedError(M, "busy"))
    rt = runtime(db, handler, ch, max_turn_attempts=1)  # a poison guard of one does not apply
    rt.on_delay, rt.on_failure = said, said
    receive(rt, [msg("w1", "selam", T0)])

    await rt.work_once()
    row = inbox(rt)
    assert row.processed_at is None and row.retry_at is not None  # put off, not given up
    assert row.error.startswith("model_unavailable")
    assert await rt.work_once() == 0  # it waits
    due_now(rt)
    await rt.work_once()  # down again: put off again, and the person is not told twice
    due_now(rt)
    await rt.work_once()  # back
    assert handler.tries == 3
    assert inbox(rt).processed_at is not None
    assert await sent(rt, ch) == ["late: model_unavailable", "cevap"]


async def test_credits_that_ran_out_stop_the_turn_at_once(db):
    ch = FakeChannel()
    handler = Flaky(ai.ModelQuotaExceededError(M, "no credits"))
    rt = runtime(db, handler, ch)
    rt.on_delay, rt.on_failure = said, said
    receive(rt, [msg("w1", "selam", T0)])
    await rt.work_once()
    assert handler.tries == 1
    assert inbox(rt).error.startswith("gave up, quota_exhausted")
    assert await sent(rt, ch) == ["gave up: quota_exhausted"]


async def test_a_failure_nobody_names_is_tried_a_few_times_then_given_up(db):
    ch = FakeChannel()
    handler = Flaky(*(RuntimeError("a bug") for _ in range(5)))
    rt = runtime(db, handler, ch, max_turn_attempts=3)
    rt.on_delay, rt.on_failure = None, said
    receive(rt, [msg("w1", "selam", T0)])
    for _ in range(5):
        await rt.work_once()
    assert handler.tries == 3
    assert await sent(rt, ch) == ["gave up: error"]


async def test_a_turn_is_given_up_when_the_channel_no_longer_lets_us_answer(db):
    ch = FakeChannel()
    handler = Flaky(ai.ModelOverloadedError(M, "busy"))
    rt = runtime(db, handler, ch)
    rt.rules = DEFAULT.but(whatsapp={"reply_window": timedelta(seconds=10)})  # shorter than a wait
    rt.on_delay, rt.on_failure = said, said
    receive(rt, [msg("w1", "selam", T0)])
    await rt.work_once()
    assert inbox(rt).processed_at is not None
    assert await sent(rt, ch) == ["gave up: model_unavailable"]


async def test_a_new_message_tries_a_turn_that_was_put_off_at_once(db):
    ch = FakeChannel()
    handler = Flaky(ai.ModelOverloadedError(M, "busy"))
    rt = runtime(db, handler, ch)
    receive(rt, [msg("w1", "selam", T0)])
    await rt.work_once()
    assert await rt.work_once() == 0
    receive(rt, [msg("w2", "orada mısınız", T0 + timedelta(seconds=40))])
    assert await rt.work_once() == 1  # both messages, in one turn
    assert await sent(rt, ch) == ["cevap"]
