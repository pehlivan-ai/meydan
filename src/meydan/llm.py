"""Model layer. Agents ask for a model here and never import a provider themselves.

A model is named by a spec string, so switching provider or model is a config change,
not a code change:

    openrouter:deepseek/deepseek-v4.1-flash
    vertex:gemini-3-flash-preview@global

Every model gets the same resilience stack, outermost first:

1. FallbackModel: if a model still fails after its retries, try the next spec.
2. ConcurrencyLimitedModel: at most `max_concurrency` requests in flight per provider; up to
   `max_queued` more wait in line, beyond that requests are rejected right away instead of
   piling up (backpressure).
3. HTTP retries, one layer only (SDK-level retries are switched off): 408/409/425/429/5xx and
   network errors, honouring `Retry-After`, otherwise exponential backoff with full jitter,
   capped per attempt and overall.
4. Timeouts on connect/read/write/pool.

Requires the `llm` extra: `uv add "meydan[llm]"`.
"""

from functools import cached_property

import httpx
from openai import AsyncOpenAI
from pydantic import AliasChoices, Field, SecretStr
from pydantic_ai import ConcurrencyLimitedModel, ConcurrencyLimiter
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings
from pydantic_ai.providers.google_cloud import GoogleCloudProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.retries import AsyncTenacityTransport, RetryConfig, wait_retry_after
from pydantic_settings import BaseSettings, SettingsConfigDict
from tenacity import (
    retry_if_exception,
    stop_after_attempt,
    stop_after_delay,
    wait_random_exponential,
)

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class LLMSettings(BaseSettings):
    """Read from the environment (`MEYDAN_LLM_*`) or passed in directly."""

    model_config = SettingsConfigDict(env_prefix="MEYDAN_LLM_", env_file=".env", extra="ignore")

    model: str  # primary spec
    fallback_models: list[str] = []  # tried in order when the primary still fails
    max_concurrency: int = 8  # in-flight requests per provider
    max_queued: int = 100  # waiting requests beyond that; more are rejected (backpressure)
    max_attempts: int = 4  # per HTTP request, including the first
    max_retry_seconds: float = 120  # per HTTP request, all attempts
    request_timeout_s: float = 30  # per attempt
    app_title: str = "meydan"  # shown in provider dashboards

    openrouter_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("MEYDAN_LLM_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
    )
    # People's messages go through these models: never route to providers that train on them,
    # and only to providers that support every parameter we send (tools, structured output).
    openrouter_provider: dict = {
        "data_collection": "deny",
        "require_parameters": True,
        "allow_fallbacks": True,
    }
    gcp_project: str | None = None  # for vertex: specs


def parse_spec(spec: str) -> tuple[str, str, str | None]:
    """'provider:model[@location]' -> (provider, model, location)."""
    provider, _, rest = spec.partition(":")
    name, _, location = rest.partition("@")
    if not provider or not name:
        raise ValueError(f"Bad model spec {spec!r}; expected '<provider>:<model>[@<location>]'.")
    return provider, name, location or None


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    return isinstance(exc, httpx.TimeoutException | httpx.NetworkError | httpx.RemoteProtocolError)


def _raise_if_retryable(response: httpx.Response) -> None:
    if response.status_code in RETRYABLE_STATUS:
        response.raise_for_status()


def retrying_transport(
    settings: LLMSettings, wrapped: httpx.AsyncBaseTransport | None = None
) -> AsyncTenacityTransport:
    # Connection pool limits only take effect on the inner transport, so set them there.
    inner = wrapped or httpx.AsyncHTTPTransport(
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20)
    )
    return AsyncTenacityTransport(
        config=RetryConfig(
            retry=retry_if_exception(_is_retryable),
            # Full jitter spreads retries out so many clients don't retry in lockstep.
            wait=wait_retry_after(
                fallback_strategy=wait_random_exponential(multiplier=0.5, max=20), max_wait=60
            ),
            stop=stop_after_attempt(settings.max_attempts)
            | stop_after_delay(settings.max_retry_seconds),
            reraise=True,
        ),
        wrapped=inner,
        validate_response=_raise_if_retryable,
    )


class LLM:
    """Builds resilient models from specs. Create one per process and reuse it: it owns the
    shared HTTP client and the per-provider concurrency limiters."""

    def __init__(self, settings: LLMSettings | None = None):
        self.settings = settings or LLMSettings()
        self._limiters: dict[str, ConcurrencyLimiter] = {}

    @cached_property
    def http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=retrying_transport(self.settings),
            timeout=httpx.Timeout(self.settings.request_timeout_s, connect=5, pool=10),
        )

    def _limiter(self, provider: str) -> ConcurrencyLimiter:
        if provider not in self._limiters:
            self._limiters[provider] = ConcurrencyLimiter(
                self.settings.max_concurrency,
                max_queued=self.settings.max_queued,
                name=f"llm:{provider}",
            )
        return self._limiters[provider]

    def _openrouter(self, name: str) -> Model:
        key = self.settings.openrouter_api_key
        if key is None or not key.get_secret_value():
            raise RuntimeError("OPENROUTER_API_KEY is not set.")
        client = AsyncOpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=key.get_secret_value(),
            http_client=self.http_client,
            max_retries=0,  # retries live in the transport, not here as well
            default_headers={"X-Title": self.settings.app_title},
        )
        return OpenRouterModel(
            name,
            provider=OpenRouterProvider(openai_client=client),
            settings=OpenRouterModelSettings(openrouter_provider=self.settings.openrouter_provider),
        )

    def _vertex(self, name: str, location: str | None) -> Model:
        if not self.settings.gcp_project:
            raise RuntimeError("MEYDAN_LLM_GCP_PROJECT is not set (needed for vertex: specs).")
        provider = GoogleCloudProvider(
            project=self.settings.gcp_project,
            location=location or "global",
            http_client=self.http_client,
        )
        return GoogleModel(name, provider=provider)

    def model(self, spec: str) -> Model:
        """One spec, with concurrency limiting and HTTP retries (no fallback)."""
        provider, name, location = parse_spec(spec)
        if provider == "openrouter":
            model = self._openrouter(name)
        elif provider == "vertex":
            model = self._vertex(name, location)
        else:
            raise ValueError(f"Unknown model provider {provider!r} in {spec!r}.")
        return ConcurrencyLimitedModel(model, limiter=self._limiter(provider))

    def get_model(self, spec: str | None = None, fallbacks: list[str] | None = None) -> Model:
        """The model to hand to an Agent: primary plus fallbacks, all resilience applied."""
        specs = [
            spec or self.settings.model,
            *(self.settings.fallback_models if fallbacks is None else fallbacks),
        ]
        models = [self.model(s) for s in specs]
        return models[0] if len(models) == 1 else FallbackModel(*models)
