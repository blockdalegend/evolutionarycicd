"""Pydantic models for telemetry records.

Telemetry captures every agent execution so the Pipeline Optimizer Agent (and
humans) can later reason about what agents actually did. Records must never
contain secrets or access tokens.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AgentTelemetryRecord(BaseModel):
    """A single, auditable record of one agent execution."""

    timestamp: datetime = Field(default_factory=_utcnow)
    agent: str
    repository: str | None = None
    pull_request: int | None = None
    commit: str | None = None
    observation: str | None = None
    decision: str | None = None
    tool: str | None = None
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    tool_result: str | None = None
    validation: str | None = None
    confidence: float | None = None
    requires_approval: bool = False
    duration_ms: float | None = None
    success: bool = True


class PipelineRun(BaseModel):
    """A single historical GitHub Actions run, as consumed by the optimizer."""

    run_id: str
    workflow: str
    status: str
    duration_seconds: float
    failed_stage: str | None = None
    failure_reason: str | None = None
    retry_count: int = 0
    agent_actions: list[str] = Field(default_factory=list)
    timestamp: datetime
