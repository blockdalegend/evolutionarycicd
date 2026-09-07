"""Supply Chain Agent.

Consumes results from deterministic security tools (Bandit, pip-audit, Zizmor,
and requirements/actions pin-checks) rather than pretending an LLM can scan
for vulnerabilities itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.base import AgentContext, AgentDecision, BaseAgent
from tools.security.scanners import (
    find_unpinned_actions,
    find_unpinned_requirements,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class SupplyChainAgent(BaseAgent):
    """Reports on dependency pinning, GitHub Actions pinning, and scan findings."""

    name = "supply_chain_agent"

    def observe(self, context: AgentContext) -> dict[str, Any]:
        unpinned_requirements = find_unpinned_requirements(REPO_ROOT / "requirements.txt")
        unpinned_actions = find_unpinned_actions(REPO_ROOT / ".github" / "workflows")
        scanner_findings = context.security_findings or {}
        action_findings = scanner_findings.get("github_actions", [])
        action_scan_status = scanner_findings.get("github_actions_scan_status", "unavailable")
        return {
            "unpinned_requirements": unpinned_requirements,
            "unpinned_actions": unpinned_actions,
            "pip_audit_findings": scanner_findings.get("pip_audit", []),
            "bandit_findings": scanner_findings.get("bandit", []),
            "github_actions_findings": action_findings,
            "github_actions_scan_status": action_scan_status,
        }

    def reason(self, context: AgentContext, observation: dict[str, Any]) -> AgentDecision:
        issues = []
        if observation["unpinned_requirements"]:
            issues.append(f"{len(observation['unpinned_requirements'])} unpinned dependency(ies)")
        if observation["unpinned_actions"]:
            issues.append(f"{len(observation['unpinned_actions'])} unpinned GitHub Action(s)")
        if observation["pip_audit_findings"]:
            issues.append(f"{len(observation['pip_audit_findings'])} known-vulnerable package(s)")
        if observation["github_actions_findings"]:
            issues.append(
                f"{len(observation['github_actions_findings'])} GitHub Actions "
                "vulnerability finding(s)"
            )
        if observation["github_actions_scan_status"] == "unavailable":
            issues.append("GitHub Actions vulnerability scan unavailable")

        if not issues:
            fallback = AgentDecision(
                action="no_action",
                reason="No supply-chain issues detected by deterministic scanners.",
                confidence=0.6,
                requires_approval=False,
            )
        else:
            fallback = AgentDecision(
                action="report_supply_chain_findings",
                reason="Supply-chain scan found: " + "; ".join(issues) + ".",
                tool="comment_pull_request",
                arguments={"issue_count": len(issues)},
                confidence=0.8,
                requires_approval=False,
            )
        return self.reason_with_llm(
            context, observation, fallback, "supply_chain.md", ["comment_pull_request"]
        )

    def act(self, context: AgentContext, decision: AgentDecision) -> dict[str, Any]:
        return {"reported": decision.tool == "comment_pull_request"}

    def validate(
        self, context: AgentContext, decision: AgentDecision, tool_result: dict[str, Any]
    ) -> dict[str, Any]:
        return {"reported": tool_result.get("reported", False)}
