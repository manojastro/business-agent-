"""Real model providers: Azure OpenAI and generic OpenAI-compatible chat-completions endpoints.

Uses plain HTTPS (httpx) so no SDK version pins the API shape. Configurable endpoint, model
(deployment), timeout, retry policy and per-1k-token prices (no prices are hardcoded).
"""

from __future__ import annotations

import json
import random
import time
from decimal import Decimal

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.providers.base import (
    ModelRequest,
    ModelResult,
    ProviderError,
    ProviderOutputInvalid,
    ProviderTimeout,
    ProviderTransient,
    Usage,
    price,
)

TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504}


class ChatCompletionsProvider:
    simulated = False

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self.name = settings.model_provider
        self.model = settings.model_name
        self.timeout = settings.model_timeout_seconds
        self.max_retries = settings.model_max_retries
        self._client = httpx.Client(timeout=self.timeout, transport=transport)

    # -- endpoint shapes -------------------------------------------------------------
    def _url_and_headers(self) -> tuple[str, dict[str, str]]:
        base = self.settings.model_endpoint.rstrip("/")
        if self.name == "azure_openai":
            url = (
                f"{base}/openai/deployments/{self.settings.model_name}/chat/completions"
                f"?api-version={self.settings.model_api_version}"
            )
            return url, {"api-key": self.settings.model_api_key}
        return f"{base}/chat/completions", {"Authorization": f"Bearer {self.settings.model_api_key}"}

    def _body(self, request: ModelRequest, repair_note: str | None) -> dict[str, object]:
        messages = [
            {"role": "system", "content": request.system},
            {"role": "user", "content": request.user_message()},
        ]
        if repair_note:
            messages.append({"role": "user", "content": repair_note})
        body: dict[str, object] = {
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        if self.name != "azure_openai":
            body["model"] = self.settings.model_name
        return body

    # -- call with bounded retry -----------------------------------------------------
    def _post(self, body: dict[str, object]) -> dict[str, object]:
        url, headers = self._url_and_headers()
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.post(url, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                last = ProviderTimeout(f"model call timed out after {self.timeout}s")
                last.__cause__ = exc
            except httpx.TransportError as exc:
                last = ProviderTransient(f"transport error: {type(exc).__name__}")
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in TRANSIENT_STATUS:
                    last = ProviderTransient(f"HTTP {resp.status_code}")
                else:
                    # Never echo the response body: it may contain the prompt.
                    raise ProviderError(f"model endpoint returned HTTP {resp.status_code}")
            if attempt < self.max_retries:
                time.sleep(min(8.0, 0.5 * 2**attempt) + random.uniform(0, 0.25))
        assert last is not None
        raise last

    def complete(self, request: ModelRequest) -> ModelResult:
        started = time.perf_counter()
        usage = Usage()
        repair_note: str | None = None
        attempts = 0
        for _ in range(2):  # one schema-repair round
            attempts += 1
            payload = self._post(self._body(request, repair_note))
            u = payload.get("usage") or {}
            usage.input_tokens += int(u.get("prompt_tokens", 0))  # type: ignore[union-attr]
            usage.output_tokens += int(u.get("completion_tokens", 0))  # type: ignore[union-attr]
            try:
                content = payload["choices"][0]["message"]["content"]  # type: ignore[index]
                data = request.response_model.model_validate(json.loads(content))
                return ModelResult(
                    data=data,
                    usage=usage,
                    cost=price(usage, self.settings.model_input_cost_per_1k, self.settings.model_output_cost_per_1k),
                    provider=self.name,
                    model=self.model,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    simulated=False,
                    attempts=attempts,
                )
            except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValidationError) as exc:
                repair_note = (
                    "Your previous reply did not validate against the schema "
                    f"({type(exc).__name__}). Reply again with only the corrected JSON object."
                )
        raise ProviderOutputInvalid("model output did not validate after one repair attempt")

    def cost_of(self, usage: Usage) -> Decimal:
        return price(usage, self.settings.model_input_cost_per_1k, self.settings.model_output_cost_per_1k)
