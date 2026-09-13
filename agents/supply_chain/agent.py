"""Supply Chain Agent.

Consumes results from deterministic security tools (Bandit, pip-audit, Zizmor,
and requirements/actions pin-checks) rather than pretending an LLM can scan
for vulnerabilities itself.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agents.base import AgentContext, AgentDecision, BaseAgent
from tools.security.scanners import (
    find_unpinned_actions,
    find_unpinned_requirements,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
logger = logging.getLogger(__name__)


class SupplyChainChange(BaseModel):
    """One complete, evidence-backed dependency or workflow file change."""

    path: str = Field(pattern=r"^(requirements\.txt|\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml)$")
    content: str = Field(min_length=20)
    rationale: str = Field(min_length=20)
    finding_locations: list[str] = Field(min_length=1, max_length=5)


class SupplyChainAgent(BaseAgent):
    """Reports on dependency pinning, GitHub Actions pinning, and scan findings."""

    @staticmethod
    def _finding_detail(tool: str, finding: Any) -> str:
        """Render scanner fields without inventing missing security details."""
        if isinstance(finding, str):
            return f"{tool}: {finding}"
        if not isinstance(finding, dict):
            return f"{tool}: {finding}"
        if tool == "pip-audit":
            package = finding.get("name") or finding.get("package") or "package not supplied"
            version = finding.get("version", "version not supplied")
            vulnerabilities = finding.get("vulns", [])
            if not vulnerabilities:
                return (
                    f"{tool}: {package}; installed version: {version}; "
                    "vulnerability details not supplied"
                )
            details = []
            for vulnerability in vulnerabilities:
                if not isinstance(vulnerability, dict):
                    details.append(str(vulnerability))
                    continue
                identifier = vulnerability.get("id", "ID not supplied")
                aliases = vulnerability.get("aliases") or []
                fix_versions = vulnerability.get("fix_versions") or []
                description = vulnerability.get("description") or "description not supplied"
                details.append(
                    f"{identifier}; aliases: {aliases or 'not supplied'}; "
                    f"fix versions: {fix_versions or 'not supplied'}; description: {description}"
                )
            return (
                f"{tool}: {package}; installed version: {version}; "
                + " | ".join(details)
            )
        if tool == "Bandit":
            identifier = finding.get("test_id") or finding.get("issue_id") or "finding"
            issue = finding.get("issue_text") or "issue text not supplied"
            location = finding.get("filename", "location not supplied")
            line = finding.get("line_number")
            if line is not None:
                location = f"{location}:{line}"
            return (
                f"{tool}: {identifier}; severity: {finding.get('issue_severity', 'not supplied')}; "
                f"confidence: {finding.get('issue_confidence', 'not supplied')}; "
                f"location: {location}; issue: {issue}"
            )
        identifier = (
            finding.get("ident")
            or finding.get("id")
            or finding.get("name")
            or finding.get("package")
            or finding.get("dependency")
            or "finding"
        )
        details = [f"{tool}: {identifier}"]
        for label, keys in (
            ("severity", ("severity",)),
            ("confidence", ("confidence",)),
            ("location", ("location", "filename", "file", "line")),
            ("CVE", ("cve", "CVE")),
            ("URL", ("url", "remediation_url", "reference")),
        ):
            value = next((finding[key] for key in keys if finding.get(key)), None)
            determinations = finding.get("determinations")
            if value is None and label in {"severity", "confidence"} and isinstance(
                determinations, dict
            ):
                value = determinations.get(label)
            if value is not None:
                details.append(f"{label}: {value}")
        return "; ".join(details)

    @classmethod
    def _render_scanner_details(cls, observation: dict[str, Any]) -> str:
        """Append deterministic findings so concise LLM summaries remain actionable."""
        sections = ["Deterministic scanner details:"]
        for tool, key in (
            ("requirements pin-check", "unpinned_requirements"),
            ("Actions pin-check", "unpinned_actions"),
            ("pip-audit", "pip_audit_findings"),
            ("Bandit", "bandit_findings"),
            ("Zizmor", "github_actions_findings"),
        ):
            findings = observation.get(key, [])
            if findings:
                sections.append(f"- {tool} ({len(findings)} finding(s)):")
                sections.extend(f"  - {cls._finding_detail(tool, finding)}" for finding in findings)
            elif key not in {"github_actions_findings"}:
                sections.append(f"- {tool}: no findings")
        sections.append(
            "- Zizmor scan status: "
            f"{observation.get('github_actions_scan_status', 'unavailable')}"
        )
        return "\n".join(sections)

    @staticmethod
    def _render_recommendations(observation: dict[str, Any]) -> str:
        """Provide conservative remediation guidance even when the LLM is unavailable."""
        recommendations = ["Recommendations:"]
        for finding in observation.get("pip_audit_findings", []):
            if not isinstance(finding, dict):
                continue
            package = finding.get("name") or finding.get("package", "package not supplied")
            for vulnerability in finding.get("vulns", []):
                if not isinstance(vulnerability, dict):
                    continue
                identifier = vulnerability.get("id", "ID not supplied")
                fix_versions = vulnerability.get("fix_versions") or []
                if fix_versions:
                    recommendations.append(
                        f"- {identifier} ({package}): upgrade to one of the supplied fixed "
                        f"versions: {fix_versions}."
                    )
                else:
                    recommendations.append(
                        f"- {identifier} ({package}): no fixed version was supplied; "
                        "review the advisory before changing the dependency."
                    )
        for finding in observation.get("bandit_findings", []):
            if isinstance(finding, dict):
                identifier = finding.get("test_id") or finding.get("issue_id", "finding")
                location = finding.get("filename", "location not supplied")
                line = finding.get("line_number")
                if line is not None:
                    location = f"{location}:{line}"
                recommendations.append(
                    f"- {identifier} at {location}: review the reported issue and apply the "
                    "least-privilege or input-handling fix appropriate to the code."
                )
        for finding in observation.get("github_actions_findings", []):
            if not isinstance(finding, dict):
                continue
            identifier = finding.get("ident", finding.get("id", "finding"))
            url = finding.get("url") or finding.get("remediation_url") or "URL not supplied"
            if identifier == "unpinned-uses":
                action = "replace each mutable action tag with a reviewed full commit SHA"
            elif identifier == "dangerous-triggers":
                action = "review the workflow trigger and untrusted-input permissions"
            elif identifier == "artipacked":
                action = "review artifact handling and prevent untrusted artifact overwrite"
            else:
                action = "follow the scanner guidance for this audit"
            recommendations.append(f"- {identifier}: {action}; reference: {url}.")
        if not observation.get("pip_audit_findings") and not observation.get("bandit_findings") \
                and not observation.get("github_actions_findings"):
            recommendations.append("- No scanner-specific remediation is required.")
        return "\n".join(recommendations)

    name = "supply_chain_agent"

    def observe(self, context: AgentContext) -> dict[str, Any]:
        unpinned_requirements = find_unpinned_requirements(REPO_ROOT / "requirements.txt")
        unpinned_actions = find_unpinned_actions(REPO_ROOT / ".github" / "workflows")
        scanner_findings = context.security_findings or {}
        action_findings = scanner_findings.get("github_actions", [])
        action_scan_status = scanner_findings.get("github_actions_scan_status", "unavailable")
        repair_files = {
            "requirements.txt": (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
        }
        for workflow in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
            repair_files[str(workflow.relative_to(REPO_ROOT))] = workflow.read_text(
                encoding="utf-8"
            )
        return {
            "unpinned_requirements": unpinned_requirements,
            "unpinned_actions": unpinned_actions,
            "pip_audit_findings": scanner_findings.get("pip_audit", []),
            "bandit_findings": scanner_findings.get("bandit", []),
            "github_actions_findings": action_findings,
            "github_actions_scan_status": action_scan_status,
            "repair_files": repair_files,
        }

    @staticmethod
    def _validate_proposed_changes(
        proposed_changes: Any, observation: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Validate model-authored files before they can reach GitHub."""
        if not isinstance(proposed_changes, list):
            raise ValueError("proposed_changes must be a list")
        repair_files = observation.get("repair_files", {})
        evidence = json.dumps(observation, default=str)
        validated: list[dict[str, Any]] = []
        for raw_change in proposed_changes:
            change_model = SupplyChainChange.model_validate(raw_change)
            if change_model.path not in repair_files:
                raise ValueError(
                    f"proposed change is not an existing repair file: {change_model.path}"
                )
            if "```" in change_model.content:
                raise ValueError(
                    f"proposed change contains a markdown fence: {change_model.path}"
                )
            if not all(location in evidence for location in change_model.finding_locations):
                raise ValueError(
                    f"proposed change cites unsupported evidence: {change_model.path}"
                )
            validated.append(change_model.model_dump())

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            for change in validated:
                candidate = temporary_root / change["path"]
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_text(change["content"], encoding="utf-8")
            requirements = temporary_root / "requirements.txt"
            if requirements.exists() and find_unpinned_requirements(requirements):
                raise ValueError("proposed requirements.txt still contains unpinned entries")
            workflows = temporary_root / ".github" / "workflows"
            if workflows.exists() and find_unpinned_actions(workflows):
                raise ValueError("proposed workflows still contain unpinned actions")
        return validated

    def reason(self, context: AgentContext, observation: dict[str, Any]) -> AgentDecision:
        issues = []
        if observation["unpinned_requirements"]:
            issues.append(f"{len(observation['unpinned_requirements'])} unpinned dependency(ies)")
        if observation["unpinned_actions"]:
            issues.append(f"{len(observation['unpinned_actions'])} unpinned GitHub Action(s)")
        if observation["pip_audit_findings"]:
            issues.append(f"{len(observation['pip_audit_findings'])} known-vulnerable package(s)")
        if observation["bandit_findings"]:
            issues.append(f"{len(observation['bandit_findings'])} Bandit security finding(s)")
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
        decision = self.reason_with_llm(
            context,
            observation,
            fallback,
            "supply_chain.md",
            ["comment_pull_request", "create_pull_request"],
            preserve_argument_keys=("proposed_changes",),
        )
        try:
            proposed_changes = self._validate_proposed_changes(
                decision.arguments.get("proposed_changes", []), observation
            )
        except (TypeError, ValueError) as exc:
            logger.warning("Ignoring invalid supply-chain proposed changes: %s", exc)
            proposed_changes = []
        decision.arguments["proposed_changes"] = proposed_changes
        if proposed_changes:
            decision.action = "propose_supply_chain_fix_pull_request"
            decision.tool = "create_pull_request"
            decision.requires_approval = True
        decision.reason = (
            decision.reason
            + "\n\n"
            + self._render_scanner_details(observation)
            + "\n\n"
            + self._render_recommendations(observation)
        )
        return decision

    def act(self, context: AgentContext, decision: AgentDecision) -> dict[str, Any]:
        proposed_changes = decision.arguments.get("proposed_changes", [])
        if decision.tool == "create_pull_request" and proposed_changes:
            from tools.github.client import GitHubClient

            files = {change["path"]: change["content"] for change in proposed_changes}
            if os.environ.get("AGENT_ALLOW_FIX_PR", "false").lower() not in {"1", "true", "yes"}:
                logger.info("Supply-chain fix PR requires AGENT_ALLOW_FIX_PR=true")
                return {
                    "created": False,
                    "dry_run": True,
                    "approval_required": True,
                    "files": sorted(files),
                }
            client = GitHubClient()
            if client.dry_run:
                return {
                    "created": False,
                    "dry_run": True,
                    "files": sorted(files),
                }
            if not context.repository:
                return {"created": False, "error": "repository is required to create a PR"}
            branch = f"agent/supply-chain-{context.commit_sha or 'fix'}"[:60]
            return client.create_pull_request_with_files(
                branch_name=branch,
                base_branch="main",
                title="Supply Chain Agent: apply recommended fixes",
                body=decision.reason,
                files=files,
                repository=context.repository,
            )
        return {"reported": decision.tool == "comment_pull_request"}

    def validate(
        self, context: AgentContext, decision: AgentDecision, tool_result: dict[str, Any]
    ) -> dict[str, Any]:
        if decision.tool == "create_pull_request":
            return {
                "pull_request_created": tool_result.get("created", False),
                "pull_request_url": tool_result.get("url"),
                "files": tool_result.get("files", []),
            }
        return {"reported": tool_result.get("reported", False)}
