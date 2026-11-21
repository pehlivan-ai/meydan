"""meydan.media: files kept once by their SHA-256, and the sender turning a key into what the
channel takes (an upload id reused per sending number, or the file itself)."""

from datetime import timedelta

import pytest
from conftest import T0, FakeChannel, msg, receive, runtime
from sqlalchemy import func, select, update

from meydan import media
from meydan.channels.base import Outbound

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def test_the_same_bytes_are_one_file(db):
    engine, t = db
    with engine.begin() as c:
        a = media.put(c, t, PNG, "image/png")
        b = media.put(c, t, PNG, "image/png")
        assert a == b == media.key_of(PNG)
        assert c.execute(select(t.media)).one().size == len(PNG)
        assert media.load(c, t, a) == (PNG, "image/png")
        with pytest.raises(media.MediaMissingError):
            media.load(c, t, "0" * 64)
        with pytest.raises(ValueError):
            media.put(c, t, b"", "image/png")


def test_a_file_is_read_only_from_the_store_that_has_it(db):
    engine, t = db

    class Elsewhere(media.PostgresStore):
        name = "elsewhere"

    with engine.begin() as c:
        key = media.put(c, t, PNG, "image/png", store=Elsewhere(t))
        assert c.execute(select(t.media.c.store)).scalar() == "elsewhere"
        with pytest.raises(media.MediaMissingError):
            media.load(c, t, key)  # the default (postgres) store isn't the one that has it
        assert media.load(c, t, key, store=Elsewhere(t))[0] == PNG


class Uploading(FakeChannel):
    def __init__(self):
        super().__init__()
        self.uploads: list[tuple[str, int, str]] = []
        self.ids: list[str | None] = []

    async def upload(self, from_number, data, mime):
        self.uploads.append((from_number, len(data), mime))
        return f"media.{len(self.uploads)}"

    async def send(self, from_number, to, message):
        self.ids.append(message.channel_media_id)
        return await super().send(from_number, to, message)


async def test_an_image_goes_up_once_per_number_and_is_sent_by_id(db):
    engine, t = db
    with engine.begin() as c:
        key = media.put(c, t, PNG, "image/png")

    async def poster(turn):
        return [Outbound.image_message(key, alt="poster")]

    ch = Uploading()
    rt = runtime(db, poster, ch)
    for i in range(2):
        receive(rt, [msg(f"w{i}", "afiş?", T0)])
        await rt.work_once()
        await rt.send_once()
    assert ch.uploads == [("222", len(PNG), "image/png")]  # once, then the id is reused
    assert ch.ids == ["media.1", "media.1"]
    with engine.connect() as c:
        stored = c.execute(select(t.outbox.c.message)).scalars().all()
    assert all("channel_media_id" not in m for m in stored)  # never kept on the row

    with engine.begin() as c:  # Meta drops uploads after 30 days: an old id isn't used
        c.execute(update(t.media_uploads).values(uploaded_at=func.now() - timedelta(days=31)))
    receive(rt, [msg("w9", "afiş?", T0)])
    await rt.work_once()
    await rt.send_once()
    assert len(ch.uploads) == 2 and ch.ids[-1] == "media.2"


async def test_a_channel_without_uploads_gets_the_file_itself(db):
    engine, t = db
    with engine.begin() as c:
        key = media.put(c, t, PNG, "image/png")
    got = []

    class Takes(FakeChannel):
        async def send(self, from_number, to, message):
            got.append((message.media_bytes, message.media_mime))
            return await super().send(from_number, to, message)

    async def poster(turn):
        return [Outbound.image_message(key)]

    rt = runtime(db, poster, Takes())
    receive(rt, [msg("w1", "afiş?", T0)])
    await rt.work_once()
    await rt.send_once()
    assert got == [(PNG, "image/png")]


async def test_a_missing_file_is_a_dead_letter(db):
    engine, t = db

    async def poster(turn):
        return [Outbound.image_message("0" * 64)]

    rt = runtime(db, poster, Uploading())
    receive(rt, [msg("w1", "afiş?", T0)])
    await rt.work_once()
    await rt.send_once()
    with engine.connect() as c:
        row = c.execute(select(t.outbox)).one()
    assert row.status == "dead" and "no file" in row.last_error


async def test_a_templates_image_header_goes_up_like_an_image(db):
    engine, t = db
    with engine.begin() as c:
        key = media.put(c, t, PNG, "image/png")

    async def plan(turn):
        return [Outbound.template_message("daily_plan", "tr", media=key, **{"1": "Ada"})]

    ch = Uploading()
    rt = runtime(db, plan, ch)
    receive(rt, [msg("w1", "plan?", T0)])
    await rt.work_once()
    await rt.send_once()
    assert ch.uploads == [("222", len(PNG), "image/png")] and ch.ids == ["media.1"]
