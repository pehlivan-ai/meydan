"""Photos people send: a channel downloads them, the worker keeps them before the turn (made
ready for a model), the agent sees them when the app says its model reads images, observe shows
them to whom may see the conversation, and they are forgotten after the app's retention."""

import asyncio
import io
import json
import uuid
from datetime import timedelta

import httpx
import pytest
from conftest import T0, FakeChannel, receive, runtime
from PIL import Image
from pydantic_ai import Agent
from pydantic_ai.messages import BinaryContent, ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import func, select, text, update

from meydan import media
from meydan.agent import handler, history
from meydan.channels.base import Address, Inbound
from meydan.channels.telegram import Telegram
from meydan.channels.whatsapp import WhatsAppCloud
from meydan.reply import Reply
from meydan.runtime import Presence
from meydan.runtime.conversation import Said


def photo(w: int = 3000, h: int = 2000, orientation: int | None = None) -> bytes:
    """A JPEG with GPS-like metadata (an EXIF tag) and, optionally, an orientation."""
    im = Image.new("RGB", (w, h), (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # Make: stands for whatever a camera writes
    if orientation:
        exif[0x0112] = orientation
    out = io.BytesIO()
    im.save(out, "JPEG", exif=exif)
    return out.getvalue()


def image_message(mid: str, caption: str = "") -> Inbound:
    sender = Address("tel", "+905550000001")
    payload = {"id": mid, "type": "image", "image": {"id": f"media-{mid}", "caption": caption}}
    return Inbound(
        "whatsapp", mid, Address("wa-pnid", "222"), sender, T0, "image", caption, payload
    )


class PhotoChannel(FakeChannel):
    def __init__(self, data: bytes | None = None, fail: bool = False):
        super().__init__()
        self.data, self.failing, self.asked = data or photo(), fail, []

    async def download_media(self, message):
        self.asked.append(message.message_id)
        if self.failing:
            raise RuntimeError("media: 404")
        return self.data, "image/jpeg"


# --- preparing ----------------------------------------------------------------------------------


def test_a_photo_is_made_upright_small_and_without_its_metadata():
    data, mime = media.prepare_image(photo(3000, 2000, orientation=6), "image/jpeg")
    with Image.open(io.BytesIO(data)) as im:
        assert mime == "image/jpeg" and im.format == "JPEG"
        assert im.size == (1045, 1568)  # turned upright (6 = 90°), long edge 1568
        assert not im.getexif()  # the camera's tags are gone


def test_a_file_that_is_not_an_image_is_kept_as_it_came():
    assert media.prepare_image(b"not a photo", "image/jpeg") == (b"not a photo", "image/jpeg")


# --- channels -----------------------------------------------------------------------------------


async def test_whatsapp_downloads_by_media_id_with_the_token():
    seen = []

    def handle(request):
        seen.append((str(request.url), request.headers.get("authorization")))
        if request.url.path.endswith("/media-w1"):
            return httpx.Response(200, json={"url": "https://lookaside.example/x", "mime_type":
                                             "image/jpeg", "file_size": 3})  # fmt: skip
        return httpx.Response(200, content=b"abc", headers={"content-type": "image/jpeg"})

    cloud = WhatsAppCloud("token", client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    assert await cloud.download_media(image_message("w1")) == (b"abc", "image/jpeg")
    assert [a for _, a in seen] == ["Bearer token", "Bearer token"]
    assert seen[1][0] == "https://lookaside.example/x"
    text = Inbound("whatsapp", "w2", Address("wa-pnid", "1"), Address("tel", "+1"), T0, "text", "x")
    assert await cloud.download_media(text) is None


async def test_telegram_downloads_the_largest_size_of_a_photo():
    token = "111:secret"
    seen = []

    def handle(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/getFile"):
            assert json.loads(request.content) == {"file_id": "big"}
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/1.jpg"}})
        return httpx.Response(200, content=b"jpeg")

    bot = Telegram([token], client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    m = Inbound("telegram", "111:9:1", Address("tg-bot", "111"), Address("tg", "9"), T0, "image",
                "", {"photo": [{"file_id": "small"}, {"file_id": "big"}]})  # fmt: skip
    assert await bot.download_media(m) == (b"jpeg", "image/jpeg")
    assert seen[-1] == "/file/bot111:secret/photos/1.jpg"


# --- the runtime --------------------------------------------------------------------------------


async def test_the_worker_keeps_a_photo_before_the_turn_and_the_transcript_points_at_it(db):
    turns = []

    async def reply(turn):
        turns.append(turn)
        return []

    ch = PhotoChannel()
    rt = runtime(db, reply, channel=ch)
    receive(rt, [image_message("w1", "afiş bu mu?")])
    await rt.events_once()  # Ingest: fetched and kept
    await rt.work_once()
    [turn] = turns
    key = turn.messages[0].media
    assert key and ch.asked == ["w1"]
    with rt.engine.connect() as c:
        data, mime = media.load(c, rt.t, key)
        assert mime == "image/jpeg" and max(Image.open(io.BytesIO(data)).size) == 1568
        conversation = c.scalar(select(rt.t.conversations.c.id))
        [line] = rt.transcript(c, conversation)
    assert (line.text, line.media) == ("[image] afiş bu mu?", key)


async def test_a_photo_that_cannot_be_fetched_still_gets_its_turn(db):
    turns = []

    async def reply(turn):
        turns.append(turn)
        return []

    rt = runtime(db, reply, channel=PhotoChannel(fail=True), ingest_wait_s=0)
    receive(rt, [image_message("w1")])
    await rt.events_once()  # Ingest fails: tried again later; the turn doesn't wait (wait 0)
    await rt.work_once()
    assert [m.media for m in turns[0].messages] == [None]
    with rt.engine.connect() as c:
        d = c.execute(select(rt.t.event_deliveries)).one()
    assert d.status == "pending" and "404" in d.last_error  # recorded, retried with backoff


async def test_received_photos_are_forgotten_after_the_retention(db):
    async def reply(turn):
        return []

    rt = runtime(db, reply, channel=PhotoChannel(), keep_received=timedelta(days=30))
    receive(rt, [image_message("w1")])
    await rt.events_once()  # Ingest: fetched and kept
    await rt.work_once()
    assert rt.forget_received_once() == 0  # still young
    with rt.engine.begin() as c:
        c.execute(update(rt.t.inbox).values(received_at=func.now() - timedelta(days=31)))
    assert rt.forget_received_once() == 1
    with rt.engine.connect() as c:
        assert c.scalar(select(func.count()).select_from(rt.t.media)) == 0
        row = c.execute(select(rt.t.inbox.c.media, rt.t.inbox.c.text)).one()
    assert row.media is None  # the message stays, without its file


async def test_a_file_we_also_sent_is_not_forgotten(db):
    engine, t = db

    async def reply(turn):
        return []

    data = photo(100, 100)
    rt = runtime(db, reply, channel=PhotoChannel(data))
    receive(rt, [image_message("w1")])
    await rt.events_once()  # Ingest: fetched and kept
    await rt.work_once()
    with engine.begin() as c:
        key = c.scalar(select(t.inbox.c.media))
        rt.enqueue(c, channel="whatsapp", conversation_id=c.scalar(select(t.conversations.c.id)),
                   from_number="222", to="+905550000001", recipient_identity_id=None,
                   messages=[media_message(key)])  # fmt: skip
        c.execute(update(t.inbox).values(received_at=func.now() - timedelta(days=31)))
        assert media.forget_received(c, t, timedelta(days=30)) == 0
        assert c.scalar(select(func.count()).select_from(t.media)) == 1


def media_message(key):
    from meydan.channels.base import Outbound

    return Outbound.image_message(key, alt="the same photo")


# --- the agent ----------------------------------------------------------------------------------


def seeing():
    """A model that keeps the prompt it was given and the history's first message."""

    def model(messages, info):
        seeing.prompt = messages[-1].parts[0].content
        seeing.first = messages[0].parts[0].content
        [output] = info.output_tools
        return ModelResponse(parts=[ToolCallPart(output.name, {"bubbles": ["Gördüm."]})])

    return FunctionModel(model)


async def test_with_images_on_the_turns_photo_goes_to_the_model_as_bytes(db):
    engine, t = db

    async def reply(turn):
        return await agent_reply(turn)

    agent_reply = handler(Agent(seeing(), output_type=Reply), engine=engine, tables=t,
                          images=True)  # fmt: skip
    rt = runtime(db, reply, channel=PhotoChannel())
    receive(rt, [image_message("w1", "bu afiş mi?")])
    await rt.events_once()  # Ingest: fetched and kept
    await rt.work_once()
    [image, text] = seeing.prompt
    assert isinstance(image, BinaryContent) and image.media_type == "image/jpeg"
    assert text == "[image] bu afiş mi?"


async def test_without_images_the_model_reads_only_text(db):
    engine, t = db
    agent_reply = handler(Agent(seeing(), output_type=Reply), engine=engine, tables=t)

    async def reply(turn):
        return await agent_reply(turn)

    rt = runtime(db, reply, channel=PhotoChannel())
    receive(rt, [image_message("w1", "bu afiş mi?")])
    await rt.events_once()  # Ingest: fetched and kept
    await rt.work_once()
    assert seeing.prompt == "[image] bu afiş mi?"


def test_history_puts_back_only_the_photos_it_is_given():
    lines = [
        Said(T0, "in", None, "[image] eski", media="k1"),
        Said(T0, "out", None, "Gördüm."),
        Said(T0, "in", None, "[image] yeni", media="k2"),
        Said(T0, "in", None, "ne dersin?"),
    ]
    old, _, new = history(lines, {"k2": (b"jpeg", "image/jpeg")})
    assert old.parts[0].content == "[image] eski"  # not given: text only
    [image, text] = new.parts[0].content
    assert isinstance(image, BinaryContent) and text == "[image] yeni\nne dersin?"
    assert history(lines)[2].parts[0].content == "[image] yeni\nne dersin?"


# --- observe ------------------------------------------------------------------------------------


async def test_the_team_sees_a_received_photo_like_the_message_it_came_in(db):
    from meydan.auth import Principal
    from meydan.observe import query
    from meydan.visibility import Visibility

    async def reply(turn):
        return []

    rt = runtime(db, reply, channel=PhotoChannel())
    receive(rt, [image_message("w1")])
    await rt.events_once()  # Ingest: fetched and kept
    await rt.work_once()
    engine, t = db

    def who(*scopes):
        with engine.begin() as c:
            actor = c.scalar(t.actors.insert().values(kind="person").returning(t.actors.c.id))
        return Principal(actor, uuid.uuid4(), "ops", frozenset(scopes))

    reader, nobody = who("ops", "messages:read"), who("ops")
    visibility = Visibility.trusted("ops")
    with engine.begin() as c:
        key = c.scalar(select(t.inbox.c.media))
        conversation = c.scalar(select(t.conversations.c.id))
        assert query.media(c, t, reader, key, visibility=visibility)[1] == "image/jpeg"
        assert query.media(c, t, nobody, key, visibility=Visibility.need_to_know()) is None
        line = query.conversation(c, t, reader, conversation, visibility=visibility).lines[0]
    assert line.image and line.image.media == key


# --- a photo sent "as a file" ------------------------------------------------------------------


def document_message(mid: str, mime: str, caption: str = "") -> Inbound:
    sender, to = Address("tel", "+905550000001"), Address("wa-pnid", "222")
    payload = {"id": mid, "type": "document", "document": {
        "id": f"media-{mid}", "mime_type": mime, "filename": "x", "caption": caption}}  # fmt: skip
    return Inbound("whatsapp", mid, to, sender, T0, "document", caption, payload)


def test_a_document_that_is_an_image_is_seen_as_a_photo():
    from meydan.channels.base import seen_kind

    assert seen_kind("document", {"document": {"mime_type": "image/png"}}) == "image"
    assert seen_kind("document", {"document": {"mime_type": "application/pdf"}}) == "document"
    assert seen_kind("image", None) == "image"


async def test_a_photo_sent_as_a_file_is_kept_and_read_as_a_photo_a_pdf_is_not(db):
    turns = []

    async def reply(turn):
        turns.append(turn)
        return []

    ch = PhotoChannel()
    rt = runtime(db, reply, channel=ch)
    receive(rt, [document_message("d1", "image/png", "ne bu")])
    await rt.events_once()  # Ingest: fetched and kept
    await rt.work_once()
    receive(rt, [document_message("d2", "application/pdf")])
    await rt.events_once()  # Ingest: fetched and kept
    await rt.work_once()
    assert ch.asked == ["d1"]  # the PDF is not downloaded
    assert turns[0].messages[0].media and turns[1].messages[0].media is None
    with rt.engine.connect() as c:
        conversation = c.scalar(select(rt.t.conversations.c.id))
        lines = rt.transcript(c, conversation)
    assert [x.text for x in lines] == ["[image] ne bu", "[document]"]


# --- ingested, then seen, then the turn (meydan.runtime.ingest) ---------------------------------


def time_passes(rt):
    with rt.engine.begin() as c:
        c.execute(update(rt.t.event_deliveries).values(next_at=text("now() - interval '1 s'")))


async def test_a_photo_is_seen_and_answered_only_once_it_is_in_the_system(db):
    turns = []

    async def reply(turn):
        turns.append(turn)
        return []

    ch = PhotoChannel()
    rt = runtime(db, reply, channel=ch, presence=Presence(read_after_s=1, typing=False))
    receive(rt, [image_message("w1", "afiş")])
    assert await rt.work_once() == 0  # not before the photo is in
    time_passes(rt)
    assert ("read", "w1") not in ch.events
    await rt.events_once()  # Ingest; "seen" now due a second after
    assert ("read", "w1") not in ch.events
    time_passes(rt)
    await rt.events_once()
    assert ("read", "w1") in ch.events
    assert await rt.work_once() == 1 and turns[0].messages[0].media


async def test_ingest_never_waits_on_a_message_a_turn_holds(db):
    engine, t = db

    async def reply(turn):
        return []

    rt = runtime(db, reply, channel=PhotoChannel())
    receive(rt, [image_message("w1")])
    with engine.begin() as held:  # a turn that went ahead holds the row while the model thinks
        held.execute(select(t.inbox).with_for_update())
        await asyncio.wait_for(rt.events_once(), timeout=5)
    with engine.connect() as c:
        d = c.execute(select(t.event_deliveries)).one()
        assert c.scalar(select(t.inbox.c.ingested_at)) is None
    assert d.status == "pending" and d.attempts == 0  # Later: not counted, tried again soon


def test_an_album_reads_as_one():
    from meydan.channels.base import describe, seen_kind

    payload = {"type": "unsupported", "unsupported": {"type": "album"}}
    assert describe(seen_kind("unsupported", payload), "") == "[album]"


async def test_read_once_still_works_and_says_it_is_deprecated(db):
    async def reply(turn):
        return []

    rt = runtime(db, reply)
    with pytest.warns(DeprecationWarning, match="events_once"):
        assert await rt.read_once() == 0
