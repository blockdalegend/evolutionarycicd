"""Structured work orders produced by discovery agents."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator


class IssueProposal(BaseModel):
    """An evidence-backed work order for the Copilot coding agent."""

    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    requested_change: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(default_factory=list)
    validation_commands: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    source_agent: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    original_pull_request: int | None = None
    commit_sha: str | None = None
    assign_to_copilot: bool = True
    requires_human_review: bool = True
    finding_type: str = "finding"

    @field_validator("affected_files", "evidence", "acceptance_criteria", "validation_commands")
    @classmethod
    def _strip_empty(cls, values: list[str]) -> list[str]:
        return [value.strip() for value in values if value.strip()]

    def fingerprint(self) -> str:
        """Return a stable identifier for this unresolved finding."""
        parts = [
            self.source_agent,
            self.finding_type,
            *(self.affected_files or ["repository"]),
            re.sub(r"\s+", " ", self.problem.lower()).strip(),
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]

    def render_markdown(self) -> str:
        """Render a deterministic, implementation-oriented issue body."""
        def bullets(values: list[str]) -> str:
            return "\n".join(f"- [ ] {value}" for value in values) or (
                "- [ ] Complete the requested change"
            )

        evidence = "\n".join(f"- {value}" for value in self.evidence) or (
            "- Evidence was not supplied"
        )
        files = "\n".join(f"- `{value}`" for value in self.affected_files) or "- None identified"
        commands = "\n".join(f"`{value}`" for value in self.validation_commands) or "`pytest`"
        assignment = (
            "Copilot may implement the requested change and create a Pull Request."
            if self.assign_to_copilot
            else "Copilot assignment requires explicit human authorization."
        )
        return f"""# {self.title}

## Summary

{self.summary}

## Problem

{self.problem}

## Evidence

{evidence}

## Affected Files

{files}

## Requested Change

{self.requested_change}

## Acceptance Criteria

{bullets(self.acceptance_criteria)}

## Validation

Run:

{commands}

## Agent Context

Source Agent: {self.source_agent}
Confidence: {self.confidence:.2f}
Original PR: {self.original_pull_request or "none"}
Commit: {self.commit_sha or "none"}

## Authority

{assignment}

Human review is required. Copilot MUST NOT merge or deploy the change.

<!-- evolutionary-cicd:fingerprint={self.fingerprint()} -->
<!-- evolutionary-cicd:source-agent={self.source_agent} -->
"""


class EvolutionProposal(BaseModel):
    """Lifecycle record for a measurable pipeline improvement."""

    id: str
    issue_number: int | None = None
    problem: str
    hypothesis: str
    metric: str
    baseline_value: float
    target_value: float
    sample_size: int = Field(ge=1)
    target_file: str
    proposed_change: str
    status: str = "DETECTED"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def proposal_from_decision(
    source_agent: str,
    context: object,
    observation: dict[str, object],
    decision: object,
) -> IssueProposal | None:
    """Build a conservative proposal from an actionable agent decision."""
    action = getattr(decision, "action", "no_action")
    confidence = float(getattr(decision, "confidence", 0.0))
    if action == "no_action" or confidence <= 0:
        return None
    arguments = getattr(decision, "arguments", {}) or {}
    files = list(getattr(context, "changed_files", []) or [])
    if not files:
        files = list(arguments.get("files", []) or [])
    reason = getattr(decision, "reason", "Actionable finding")
    return IssueProposal(
        title=f"[{source_agent.replace('_agent', '').replace('_', ' ').title()}] {reason[:100]}",
        summary=reason,
        problem=reason,
        evidence=[f"{key}: {value}" for key, value in observation.items() if value][:8],
        affected_files=files[:20],
        requested_change=reason,
        acceptance_criteria=["Implement the requested change", "Existing tests continue to pass"],
        validation_commands=[
            "pytest",
            "ruff check .",
            "mypy app agents tools llm telemetry scripts",
        ],
        labels=["agent-recommendation"],
        source_agent=source_agent,
        confidence=confidence,
        original_pull_request=getattr(context, "pull_request_number", None),
        commit_sha=getattr(context, "commit_sha", None),
        assign_to_copilot=True,
        requires_human_review=True,
        finding_type=action,
    )
