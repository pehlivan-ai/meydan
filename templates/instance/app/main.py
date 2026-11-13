"""The running app: `uvicorn --factory app.main:create_app`.

One process: the webhook, and beside it meydan's reader, worker and sender loops. Parts are
built here, once, and passed to whoever needs them; nothing connects because it was imported.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from meydan.channels.whatsapp import WhatsAppCloud
from meydan.runtime import Runtime, RuntimeConfig, templates
from meydan.runtime.types import Channel
from sqlalchemy import create_engine

from app import webhook
from app.agent import reply
from app.config import Env
from app.tables import runtime_tables

log = logging.getLogger("app")


def create_app(
    env: Env | None = None,
    channel: Channel | None = None,  # tests pass a fake; left out: the WhatsApp Cloud API
    config: RuntimeConfig | None = None,
) -> FastAPI:
    env = env or Env()
    if channel is None and not env.whatsapp_access_token:
        # Nothing is set yet (a fresh copy, CI): the app starts and is healthy, receives nothing
        # it can verify, and says so. Set the three values in .env to talk to people.
        log.warning("WHATSAPP_ACCESS_TOKEN is not set: this app can't send")
    if not (env.meta_app_secret and env.whatsapp_verify_token):
        log.warning("META_APP_SECRET or WHATSAPP_VERIFY_TOKEN is not set: webhooks are refused")
    channel = channel or WhatsAppCloud(env.whatsapp_access_token, env.graph_api_version)
    engine = create_engine(env.database_url)
    runtime = Runtime(engine, runtime_tables, channel, reply, config)

    async def sync_templates() -> None:
        """Our templates' state from Meta, in case a webhook was missed while we were down."""
        if not (env.whatsapp_business_account_id and hasattr(channel, "list_templates")):
            return
        try:
            n = await templates.sync(
                engine, runtime_tables, channel, env.whatsapp_business_account_id
            )
            log.info("templates synced: %d", n)
        except Exception:
            log.warning("template sync failed; webhooks still keep them current", exc_info=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await sync_templates()
        stop = asyncio.Event()
        loops = asyncio.create_task(runtime.run(stop))
        yield
        stop.set()
        await loops
        if hasattr(channel, "aclose"):
            await channel.aclose()
        engine.dispose()

    # Nothing here is public: no /docs, /redoc or /openapi.json.
    app = FastAPI(title="app", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    app.include_router(webhook.routes(env, runtime))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
