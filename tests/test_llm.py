import time

import httpx
import pytest

from meydan.llm import LLM, LLMSettings, parse_spec, retrying_transport


def settings(**kw) -> LLMSettings:
    return LLMSettings(model="openrouter:x/y", openrouter_api_key="k", **kw)


def test_parse_spec():
    assert parse_spec("openrouter:deepseek/deepseek-v4.1-flash") == (
        "openrouter",
        "deepseek/deepseek-v4.1-flash",
        None,
    )
    assert parse_spec("vertex:gemini-3-flash-preview@global") == (
        "vertex",
        "gemini-3-flash-preview",
        "global",
    )
    with pytest.raises(ValueError):
        parse_spec("no-provider")


def client(handler, **kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=retrying_transport(settings(**kw), httpx.MockTransport(handler))
    )


async def test_retries_429_then_503_then_succeeds_honouring_retry_after():
    calls: list[float] = []

    def handler(request):
        calls.append(time.monotonic())
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    async with client(handler) as c:
        r = await c.get("https://example.test/")
    assert r.status_code == 200
    assert len(calls) == 3
    assert calls[1] - calls[0] >= 0.9  # waited for Retry-After


async def test_does_not_retry_client_errors():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400)

    async with client(handler) as c:
        r = await c.get("https://example.test/")
    assert r.status_code == 400
    assert len(calls) == 1


async def test_gives_up_after_max_attempts():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500)

    async with client(handler, max_attempts=3) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.get("https://example.test/")
    assert len(calls) == 3


def test_builds_fallback_chain():
    llm = LLM(settings(fallback_models=["openrouter:a/b"]))
    assert type(llm.get_model()).__name__ == "FallbackModel"
    assert type(llm.get_model(fallbacks=[])).__name__ == "ConcurrencyLimitedModel"


def test_missing_key_is_a_clear_error():
    llm = LLM(LLMSettings(model="openrouter:x/y", openrouter_api_key=""))
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        llm.get_model()
