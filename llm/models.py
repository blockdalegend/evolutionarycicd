"""Pydantic models used by the provider-neutral LLM client."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class LLMMessage(BaseModel):
    """A single chat message sent to the LLM."""

    role: str
    content: str


class LLMRequest(BaseModel):
    """A structured request to the LLM client."""

    messages: list[LLMMessage]
    response_schema: dict[str, Any] | None = None
    temperature: float = 0.2
    max_tokens: int = 1024


class LLMResponse(BaseModel):
    """The result of an LLM call."""

    success: bool
    content: str | None = None
    parsed: dict[str, Any] | None = None
    error: str | None = None
