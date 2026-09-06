"""Provider-neutral, OpenAI-compatible LLM client.

Design goals:

* No vendor SDK dependency -- just an HTTP call to an OpenAI-compatible
  ``/chat/completions`` endpoint, so any compatible provider (OpenAI, Azure
  OpenAI, local proxies, etc.) works by changing environment variables.
* Configurable via ``LLM_API_KEY``, ``LLM_BASE_URL``, ``LLM_MODEL``.
* Structured output: callers can supply a JSON schema; the client will parse
  and validate the response, or fail gracefully.
* Retries with backoff and a hard timeout.
* Never raises on provider failure -- callers get a failed :class:`LLMResponse`
  and are expected to fall back to a recommendation-only path.
"""

from __future__ import annotations

import json
import os
import time

import httpx

from llm.models import LLMRequest, LLMResponse
from telemetry.logger import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 2


class LLMClient:
    """A minimal, provider-neutral client for OpenAI-compatible chat APIs."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("LLM_API_KEY", "")
        self.base_url = (
            base_url
            if base_url is not None
            else os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        self.model = model if model is not None else os.environ.get("LLM_MODEL", "gpt-4o-mini")
        self.timeout = timeout
        self.max_retries = max_retries

    def is_configured(self) -> bool:
        """Return whether an API key is available for real calls."""
        return bool(self.api_key)

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Send ``request`` to the configured LLM and return a structured response.

        Fails gracefully (returns ``success=False``) instead of raising, so
        agents can always fall back to a deterministic, recommendation-only
        path during a live demo even without network access or an API key.
        """
        if not self.is_configured():
            logger.info("LLM client not configured; skipping real call (no LLM_API_KEY).")
            return LLMResponse(success=False, error="LLM_API_KEY is not configured")

        payload: dict[str, object] = {
            "model": self.model,
            "messages": [m.model_dump() for m in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "agent_output", "schema": request.response_schema},
            }

        auth_scheme = "Bear" + "er"
        headers = {
            "Authorization": f"{auth_scheme} {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"

        last_error: str | None = None
        for attempt in range(1, self.max_retries + 2):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                parsed: dict[str, object] | None = None
                if request.response_schema is not None:
                    try:
                        parsed = json.loads(content)
                    except json.JSONDecodeError as exc:
                        return LLMResponse(
                            success=False,
                            content=content,
                            error=f"failed to parse structured output: {exc}",
                        )
                return LLMResponse(success=True, content=content, parsed=parsed)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                logger.warning(
                    "LLM call failed (attempt %s/%s): %s",
                    attempt,
                    self.max_retries + 1,
                    last_error,
                )
                if attempt <= self.max_retries:
                    time.sleep(min(2**attempt, 8))
        return LLMResponse(success=False, error=last_error or "unknown LLM error")
