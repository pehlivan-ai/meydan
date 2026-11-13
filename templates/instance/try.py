"""Talk to your app without WhatsApp: a fake channel, the real runtime, your `reply`.

    uv run python try.py "merhaba" "bir şey daha"     # each argument is one message
    uv run python try.py                              # type messages, one per line (Ctrl-D ends)

Needs the database (`docker compose up -d db`, then `uv run alembic upgrade head`).
"""

import asyncio
import sys
import time
from datetime import UTC, datetime

from meydan.channels.base import Address, Inbound, Outbound
from meydan.runtime import Runtime, RuntimeConfig
from sqlalchemy import create_engine, select

from app.agent import reply
from app.config import Env
from app.tables import runtime_tables

US, YOU = Address("wa-pnid", "try"), Address("tel", "+10000000000")


class Terminal:
    """The fake channel: what would go to WhatsApp is printed."""

    async def send(self, from_number: str, to: str, message: Outbound) -> str:
        print(f"  app: {message.text or f'[template {message.template}]'}")
        return f"wamid.try.{time.time_ns()}"

    async def typing(self, from_number: str, message_id: str) -> None:
        pass


async def say(runtime: Runtime, text: str) -> None:
    mid = f"try.{time.time_ns()}"
    message = Inbound("whatsapp", mid, US, YOU, datetime.now(UTC), "text", text, {"id": mid})
    with runtime.engine.begin() as conn:
        runtime.receive(conn, [message], [])
    outbox = runtime.t.outbox
    for _ in range(400):  # run turns and send replies until nothing is left
        worked = await runtime.work_once()
        await runtime.send_once()
        with runtime.engine.connect() as conn:
            pending = conn.scalar(select(outbox.c.id).where(outbox.c.status == "pending").limit(1))
        if not worked and pending is None:
            return
        await asyncio.sleep(0.01)
    raise RuntimeError("the app did not settle: a turn or a send keeps failing (see the log)")


async def main() -> None:
    fast = RuntimeConfig(debounce_s=0, max_wait_s=0, chars_per_second=1e9, presence="quiet")
    runtime = Runtime(create_engine(Env().database_url), runtime_tables, Terminal(), reply, fast)
    lines = sys.argv[1:] or (line.strip() for line in sys.stdin)
    try:
        for line in lines:
            if line:
                print(f"  you: {line}")
                await say(runtime, line)
    finally:
        runtime.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
