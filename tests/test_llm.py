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


def test_vertex_openai_spec_names_a_publisher_model_and_a_location():
    assert parse_spec("vertex-openai:deepseek-ai/deepseek-v4.1-flash-maas@global") == (
        "vertex-openai",
        "deepseek-ai/deepseek-v4.1-flash-maas",
        "global",
    )


def test_vertex_openai_url_is_global_or_regional():
    from meydan.llm import vertex_openai_url

    assert vertex_openai_url("p", "global") == (
        "https://aiplatform.googleapis.com/v1/projects/p/locations/global/endpoints/openapi"
    )
    assert vertex_openai_url("p", "europe-west4") == (
        "https://europe-west4-aiplatform.googleapis.com/v1/projects/p/locations/europe-west4"
        "/endpoints/openapi"
    )


def test_vertex_openai_refuses_a_name_without_its_publisher_and_needs_a_project():
    import pytest

    llm = LLM(LLMSettings(model="x:y", gcp_project="p"))
    with pytest.raises(ValueError, match="publisher"):
        llm.model("vertex-openai:deepseek-v4.1-flash-maas")
    with pytest.raises(RuntimeError, match="GCP_PROJECT"):
        LLM(LLMSettings(model="x:y")).model("vertex-openai:deepseek-ai/x")


async def test_google_auth_puts_a_fresh_bearer_on_each_request():
    import httpx

    from meydan.llm import GoogleAuth

    class Creds:
        def __init__(self):
            self.valid, self.token, self.refreshed = False, None, 0

        def refresh(self, request):
            self.refreshed += 1
            self.valid, self.token = True, f"t{self.refreshed}"

    auth = GoogleAuth.__new__(GoogleAuth)
    auth.credentials = Creds()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=auth) as c:
        await c.get("https://x/a")
        await c.get("https://x/b")  # still valid: no second refresh
        auth.credentials.valid = False  # expired
        await c.get("https://x/c")
    assert seen == ["Bearer t1", "Bearer t1", "Bearer t2"]
