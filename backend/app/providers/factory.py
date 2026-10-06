"""Select the configured model provider."""

from __future__ import annotations

from app.config import Settings, get_settings
from app.providers.base import ModelProvider
from app.providers.fixture import FixtureProvider


def get_provider(settings: Settings | None = None, mode: str | None = None) -> ModelProvider:
    s = settings or get_settings()
    mode = mode or s.model_mode
    if mode == "fixture":
        return FixtureProvider(delay_s=s.fixture_delay_seconds)
    if not (s.model_name and s.model_endpoint and s.model_api_key):
        raise RuntimeError("Real model mode requires MODEL_NAME, MODEL_ENDPOINT and MODEL_API_KEY")
    from app.providers.openai_compat import ChatCompletionsProvider

    return ChatCompletionsProvider(s)  # type: ignore[return-value]


def describe(provider: ModelProvider) -> dict[str, object]:
    return {
        "mode": "fixture" if provider.simulated else "real",
        "provider": provider.name,
        "model": provider.model,
        "label": "Demo simulation" if provider.simulated else f"{provider.name}:{provider.model}",
    }
