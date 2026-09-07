"""Failure Analysis Agent.

Consumes failing test output, build logs, changed files, and the PR diff to
summarize a likely root cause and suggest a remediation. It never modifies
production code -- it only reads and reports.
"""

from __future__ import annotations

from typing import Any

from agents.base import AgentContext, AgentDecision, BaseAgent


class FailureAnalysisAgent(BaseAgent):
    """Summarizes CI failures and suggests (but does not apply) fixes."""

    name = "failure_analysis_agent"

    def observe(self, context: AgentContext) -> dict[str, Any]:
        results = context.test_results or {}
        failures = results.get("failures", [])
        return {
            "failure_count": len(failures),
            "failures": failures,
            "changed_files": context.changed_files,
        }

    def reason(self, context: AgentContext, observation: dict[str, Any]) -> AgentDecision:
        failures = observation.get("failures", [])
        if not failures:
            fallback = AgentDecision(
                action="no_action",
                reason="No failing tests reported; nothing to analyze.",
                confidence=0.3,
                requires_approval=False,
            )
        else:
            likely_files = [
                f
                for f in observation.get("changed_files", [])
                if any(f in fail for fail in failures)
            ]
            suspects = likely_files or observation.get("changed_files", [])[:3]
            confidence = 0.7 if likely_files else 0.4
            fallback = AgentDecision(
                action="report_failure_analysis",
                reason=(
                    f"{len(failures)} test(s) failed. Files most likely related: "
                    f"{', '.join(suspects) or 'unknown'}."
                ),
                tool="comment_pull_request",
                arguments={"suspects": suspects},
                confidence=confidence,
                requires_approval=False,
            )
        return self.reason_with_llm(
            context, observation, fallback, "failure_analysis.md", ["comment_pull_request"]
        )

    def act(self, context: AgentContext, decision: AgentDecision) -> dict[str, Any]:
        # Reporting only; the orchestrator posts the PR comment based on the
        # decision's reason. No code modification tool is ever invoked here.
        return {"reported": True, "suspects": decision.arguments.get("suspects", [])}

    def validate(
        self, context: AgentContext, decision: AgentDecision, tool_result: dict[str, Any]
    ) -> dict[str, Any]:
        return {"reported": tool_result.get("reported", False)}
