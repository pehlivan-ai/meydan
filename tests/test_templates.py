import uuid
from datetime import timedelta

import httpx
from conftest import T0, FakeChannel, msg, receive, runtime
from sqlalchemy import select

from meydan.auth import Principal
from meydan.channels.base import Outbound, TemplateUpdate
from meydan.channels.whatsapp import WhatsAppCloud, parse_template_updates
from meydan.observe import query
from meydan.runtime import templates


async def nothing(turn):
    return []


def update(status=None, at=T0, **kw):
    return TemplateUpdate("whatsapp", "waba", "invite", "tr", at, status=status, **kw)


def queue(rt, *messages):
    """A conversation (someone wrote once), then our messages to them, in order."""
    receive(rt, [msg("in1", "hi", T0)])
    with rt.engine.begin() as c:
        conversation = c.scalar(select(rt.t.inbox.c.conversation_id))
        rt.enqueue(c, channel="whatsapp", conversation_id=conversation, from_number="222",
                   to="+905550000001", messages=list(messages))  # fmt: skip


def outbox(rt):
    with rt.engine.connect() as c:
        o = rt.t.outbox
        return c.execute(select(o.c.status, o.c.last_error).order_by(o.c.id)).all()


INVITE = Outbound.template_message("invite", "tr", name="Ada")


async def test_unknown_or_approved_templates_are_sent(db):
    channel = FakeChannel()
    rt = runtime(db, nothing, channel)
    queue(rt, INVITE)
    await rt.send_once()
    assert [s for s, _ in outbox(rt)] == ["sent"]


async def test_pending_template_is_held_out_of_line_and_sent_once_approved(db):
    channel = FakeChannel()
    rt = runtime(db, nothing, channel)
    receive(rt, [msg("in0", "hello", T0)])
    with rt.engine.begin() as c:
        templates.record(c, rt.t, update("PENDING"))
    queue(rt, INVITE, Outbound.text_message("after"))
    await rt.send_once()
    await rt.send_once()
    assert [s for s, _ in outbox(rt)] == ["held", "sent"]  # the text isn't stuck behind it
    assert "is PENDING" in outbox(rt)[0][1]

    with rt.engine.begin() as c:  # the webhook: approved
        rt.receive(c, [], [], [update("APPROVED", at=T0 + timedelta(hours=1))])
    await rt.send_once()
    assert [s for s, _ in outbox(rt)] == ["sent", "sent"]


async def test_rejected_template_gives_up_with_the_reason(db):
    rt = runtime(db, nothing, FakeChannel())
    with rt.engine.begin() as c:
        templates.record(c, rt.t, update("PAUSED"))
    queue(rt, INVITE)
    await rt.send_once()
    assert outbox(rt)[0][0] == "held"
    with rt.engine.begin() as c:
        templates.record(c, rt.t, update("REJECTED", at=T0 + timedelta(hours=1),
                                         reason="INCORRECT_CATEGORY"))  # fmt: skip
    status, why = outbox(rt)[0]
    assert status == "dead" and why == "template invite (tr) is REJECTED: INCORRECT_CATEGORY"


async def test_an_older_status_doesnt_overwrite_a_newer_one(db):
    rt = runtime(db, nothing, FakeChannel())
    with rt.engine.begin() as c:
        templates.record(c, rt.t, update("APPROVED", at=T0 + timedelta(hours=2)))
        templates.record(c, rt.t, update("PENDING", at=T0))  # a late retry of an old event
        templates.record(c, rt.t, update(quality="YELLOW", at=T0))  # other fields still apply
        row = c.execute(select(rt.t.templates)).one()
    assert row.status == "APPROVED" and row.quality == "YELLOW"


async def test_category_change_announced_then_done(db):
    rt = runtime(db, nothing, FakeChannel())
    with rt.engine.begin() as c:
        templates.record(c, rt.t, update(category="UTILITY", upcoming_category="MARKETING"))
        assert c.execute(select(rt.t.templates)).one().upcoming_category == "MARKETING"
        templates.record(c, rt.t, update(category="MARKETING"))
        row = c.execute(select(rt.t.templates)).one()
    assert (row.category, row.upcoming_category) == ("MARKETING", None)


async def test_templates_read_model_counts_held_messages(db):
    rt = runtime(db, nothing, FakeChannel())
    with rt.engine.begin() as c:
        templates.record(c, rt.t, update("PENDING", category="UTILITY"))
    queue(rt, INVITE)
    await rt.send_once()
    ops = Principal(uuid.uuid4(), uuid.uuid4(), "ops", frozenset({"ops"}))
    with rt.engine.begin() as c:
        [row] = query.templates(c, rt.t, ops)
    assert (row.name, row.status, row.held) == ("invite", "PENDING", 1)


def test_parses_the_three_template_events():
    def payload(field, value):
        return {"entry": [{"id": "waba", "time": 1793300000,
                           "changes": [{"field": field, "value": value}]}]}  # fmt: skip

    base = {"message_template_id": 7, "message_template_name": "invite",
            "message_template_language": "tr"}  # fmt: skip
    [rejected] = parse_template_updates(payload("message_template_status_update", base | {
        "event": "REJECTED", "reason": "INVALID_FORMAT", "message_template_category": "UTILITY",
        "rejection_info": {"reason": "Parameters next to each other."}}))  # fmt: skip
    assert (rejected.status, rejected.reason, rejected.category, rejected.external_id) == (
        "REJECTED",
        "Parameters next to each other.",
        "UTILITY",
        "7",
    )
    [approved] = parse_template_updates(
        payload("message_template_status_update", base | {"event": "APPROVED", "reason": "NONE"})
    )
    assert approved.status == "APPROVED" and approved.reason is None
    # Meta's own quality example uses the status field name: content decides
    [quality] = parse_template_updates(payload("message_template_status_update", base | {
        "previous_quality_score": "GREEN", "new_quality_score": "YELLOW"}))  # fmt: skip
    assert quality.quality == "YELLOW" and quality.status is None
    [soon] = parse_template_updates(payload("template_category_update", base | {
        "new_category": "UTILITY", "correct_category": "MARKETING"}))  # fmt: skip
    assert (soon.category, soon.upcoming_category) == ("UTILITY", "MARKETING")
    [done] = parse_template_updates(payload("template_category_update", base | {
        "previous_category": "UTILITY", "new_category": "MARKETING"}))  # fmt: skip
    assert (done.category, done.upcoming_category) == ("MARKETING", None)
    assert parse_template_updates(payload("messages", {"messages": []})) == []


async def test_list_templates_follows_pages():
    pages = {
        "first": {"data": [{"id": "1", "name": "a", "language": "tr", "status": "APPROVED",
                            "category": "UTILITY", "quality_score": {"score": "GREEN"}}],
                  "paging": {"next": "https://graph.facebook.com/v25.0/next"}},
        "next": {"data": [{"id": "2", "name": "b", "language": "en", "status": "PENDING",
                           "category": "MARKETING"}], "paging": {}},
    }  # fmt: skip

    def handler(request):
        page = "next" if request.url.path.endswith("/next") else "first"
        return httpx.Response(200, json=pages[page])

    cloud = WhatsAppCloud("token", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    found = await cloud.list_templates("waba")
    assert [(t.name, t.status, t.quality) for t in found] == [
        ("a", "APPROVED", "GREEN"), ("b", "PENDING", None)]  # fmt: skip
