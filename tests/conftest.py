"""Shared fixtures: a real Postgres 18 (advisory locks, SKIP LOCKED, uuidv7) and a fake channel.

Set MEYDAN_TEST_DATABASE_URL, or have a local Postgres with a `meydan_test` database.
Test data is fake (numbers, ids): this is a public repository.
"""

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import MetaData, create_engine, text

from meydan.channels.base import Address, Inbound, SendError
from meydan.runtime import Runtime, RuntimeConfig, define_tables

URL = os.environ.get(
    "MEYDAN_TEST_DATABASE_URL", "postgresql+psycopg://meydan:meydan@localhost:5432/meydan_test"
)
T0 = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


@pytest.fixture
def db():
    engine = create_engine(URL)
    try:
        with engine.connect() as c:
            c.execute(text("select uuidv7()"))
    except Exception as e:
        pytest.skip(f"no Postgres 18 test database: {e}")
    md = MetaData()
    tables = define_tables(md)
    md.drop_all(engine)
    md.create_all(engine)
    yield engine, tables
    md.drop_all(engine)
    engine.dispose()


class FakeChannel:
    def __init__(self, fail: list[SendError] | None = None):
        self.sent: list[tuple[str, str, str]] = []
        self.typing_for: list[str] = []
        self.events: list[tuple[str, str]] = []  # ("read" | "typing", message_id), in order
        self.fail = list(fail or [])

    async def send(self, from_number, to, message):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append((from_number, to, message.text))
        return f"wamid.{len(self.sent)}"

    async def typing(self, from_number, message_id):
        self.typing_for.append(message_id)
        self.events.append(("typing", message_id))

    async def mark_read(self, from_number, message_id):
        self.events.append(("read", message_id))


def msg(
    mid: str,
    body: str,
    at: datetime,
    sender: str = "+905550000001",
    to: str = "222",
    also: tuple[Address, ...] = (),
):
    who = Address("tel", sender) if sender.startswith("+") else Address("wa-bsuid", sender)
    return Inbound(
        "whatsapp", mid, Address("wa-pnid", to), who, at, "text", body, {"id": mid}, also=also
    )


def runtime(db, handler, channel=None, **cfg) -> Runtime:
    engine, tables = db
    # "instant" presence: no human-like delays in tests unless a test asks for them.
    defaults = dict(debounce_s=0, max_wait_s=0, chars_per_second=1e9, presence="instant")
    return Runtime(
        engine, tables, channel or FakeChannel(), handler, RuntimeConfig(**(defaults | cfg))
    )


def receive(rt: Runtime, messages=(), statuses=()):
    with rt.engine.begin() as c:
        return rt.receive(c, messages, statuses)
