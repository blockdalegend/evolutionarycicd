"""The Agent Orchestrator.

Keeps orchestration deliberately explicit (no multi-agent framework):

1. Load GitHub event context.
2. Read policy files (capabilities + approval rules).
3. Determine which agent should run.
4. Pass context to the agent.
5. Check any tool the agent's decision requests against policy.
6. Execute allowed tools (agents already do this in ``act``; the
   orchestrator's policy gate runs first and can veto).
7. Validate results (delegated to the agent).
8. Record telemetry (delegated to the agent's ``record`` step).
9. Post a GitHub PR comment when appropriate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from agents.base import AgentContext, AgentResult, BaseAgent
from agents.issue_proposals import IssueProposal
from telemetry.logger import get_logger
from telemetry.models import AgentTelemetryRecord
from telemetry.store import record_telemetry
from tools.github.client import GitHubClient

logger = get_logger(__name__)

POLICIES_DIR = Path(__file__).resolve().parent.parent.parent / "policies"


class PolicyViolation(RuntimeError):
    """Raised when an agent attempts an action its policy does not permit."""


def load_agent_permissions(path: Path = POLICIES_DIR / "agent_permissions.yml") -> dict[str, Any]:
    """Load the capability policy file."""
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_approval_rules(path: Path = POLICIES_DIR / "approval_rules.yml") -> dict[str, Any]:
    """Load the human-approval policy file."""
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_github_event_context() -> dict[str, Any]:
    """Load the current GitHub Actions event payload, if available.

    Reads the path referenced by ``GITHUB_EVENT_PATH``. Returns an empty dict
    when not running inside GitHub Actions (e.g. local demo).
    """
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not Path(event_path).exists():
        return {}
    return json.loads(Path(event_path).read_text(encoding="utf-8"))


class AgentOrchestrator:
    """Coordinates policy enforcement, agent execution, and reporting."""

    def __init__(
        self,
        github_client: GitHubClient | None = None,
        permissions: dict[str, Any] | None = None,
        approval_rules: dict[str, Any] | None = None,
    ) -> None:
        self.github_client = github_client or GitHubClient()
        self.permissions = permissions if permissions is not None else load_agent_permissions()
        self.approval_rules = (
            approval_rules if approval_rules is not None else load_approval_rules()
        )

    def check_capability(self, agent_name: str, capability: str) -> bool:
        """Return whether ``agent_name`` is permitted (by policy) to use ``capability``."""
        agent_policy = self.permissions.get(agent_name, {})
        return bool(agent_policy.get(capability, False))

    def requires_approval(self, capability: str) -> bool:
        """Return whether ``capability`` requires a human in the loop before acting."""
        return capability in set(self.approval_rules.get("requires_human_approval", []))

    def run_agent(self, agent: BaseAgent, context: AgentContext) -> AgentResult:
        """Run ``agent`` end-to-end, enforcing policy *before* any tool executes.

        A ``policy_check`` callback is threaded through to ``agent.run``, which
        invokes it immediately after ``reason`` produces a decision and *before*
        ``act`` is allowed to run. If the agent's declared tool capability is not
        present in its policy entry, ``PolicyViolation`` is raised at that point,
        so the disallowed side effect never executes.
        """

        def policy_check(decision: Any) -> None:
            tool = decision.tool
            if tool and not self.check_capability(agent.name, tool):
                logger.warning(
                    "policy denied tool for agent",
                    extra={"extra_fields": {"agent": agent.name, "tool": tool}},
                )
                raise PolicyViolation(f"{agent.name} is not permitted to use tool '{tool}'")
            if tool and self.requires_approval(tool):
                decision.requires_approval = True

        result = agent.run(context, policy_check=policy_check)

        issue_result: dict[str, Any] | None = None
        proposal_data = result.artifacts.get("issue_proposal")
        if proposal_data:
            issue_result = self.handle_issue_proposal(IssueProposal.model_validate(proposal_data))
            result.artifacts["issue_result"] = issue_result

        if context.pull_request_number is not None:
            comment = self._format_pr_comment(agent.name, result)
            self.github_client.post_pr_comment(context.pull_request_number, comment)
        return result

    def handle_issue_proposal(self, proposal: IssueProposal) -> dict[str, Any]:
        """Apply threshold, deduplication, and assignment policy to a proposal."""
        policy = self.approval_rules.get("issue_policy", {})
        minimum = float(policy.get("minimum_confidence", 0.8))
        agent_policy = self.permissions.get(proposal.source_agent, {})
        issue_agent_policy = policy.get("agents", {}).get(proposal.source_agent, agent_policy)
        assign = bool(
            issue_agent_policy.get(
                "assign_copilot", issue_agent_policy.get("auto_assign_copilot", False)
            )
        )
        if proposal.confidence < minimum or not issue_agent_policy.get("create_issue", False):
            record_telemetry(
                AgentTelemetryRecord(
                    agent=proposal.source_agent,
                    event="issue_proposed",
                    confidence=proposal.confidence,
                    finding_type=proposal.finding_type,
                    issue_fingerprint=proposal.fingerprint(),
                    associated_pull_request=proposal.original_pull_request,
                    success=False,
                )
            )
            logger.info(
                "issue proposal withheld",
                extra={
                    "extra_fields": {
                        "agent": proposal.source_agent,
                        "confidence": proposal.confidence,
                    }
                },
            )
            return {"created": False, "reason": "policy or confidence threshold"}
        existing = self.github_client.find_existing_issue(proposal.fingerprint())
        if existing:
            self.github_client.comment_on_issue(existing["number"], proposal.render_markdown())
            if assign:
                assignment = self.github_client.assign_issue_to_copilot_result(
                    existing["number"],
                    "Respect repository architecture, make the smallest reasonable change, "
                    "run the validation commands, create a PR, and never merge or deploy.",
                )
                existing.update(assignment)
            record_telemetry(
                AgentTelemetryRecord(
                    agent=proposal.source_agent,
                    event="issue_reused",
                    confidence=proposal.confidence,
                    finding_type=proposal.finding_type,
                    issue_number=existing["number"],
                    issue_url=existing.get("url"),
                    issue_fingerprint=proposal.fingerprint(),
                    associated_pull_request=proposal.original_pull_request,
                )
            )
            return {"created": False, "reused": True, **existing}
        created = self.github_client.create_issue_record(
            proposal.title, proposal.render_markdown(), proposal.labels
        )
        if not created.get("created"):
            record_telemetry(
                AgentTelemetryRecord(
                    agent=proposal.source_agent,
                    event="issue_created",
                    confidence=proposal.confidence,
                    finding_type=proposal.finding_type,
                    issue_fingerprint=proposal.fingerprint(),
                    success=False,
                )
            )
            return created
        if assign and (created.get("number") or self.github_client.dry_run):
            assignment = self.github_client.assign_issue_to_copilot_result(
                created.get("number", 0),
                "Respect repository architecture, make the smallest reasonable change, "
                "run the validation commands, create a PR, and never merge or deploy.",
            )
            created.update(assignment)
            record_telemetry(
                AgentTelemetryRecord(
                    agent=proposal.source_agent,
                    event=(
                        "copilot_assignment_succeeded"
                        if created["assigned"]
                        else "copilot_assignment_failed"
                    ),
                    confidence=proposal.confidence,
                    finding_type=proposal.finding_type,
                    issue_number=created.get("number"),
                    issue_url=created.get("url"),
                    issue_fingerprint=proposal.fingerprint(),
                )
            )
        record_telemetry(
            AgentTelemetryRecord(
                agent=proposal.source_agent,
                event="issue_created",
                confidence=proposal.confidence,
                finding_type=proposal.finding_type,
                issue_number=created.get("number"),
                issue_url=created.get("url"),
                issue_fingerprint=proposal.fingerprint(),
                associated_pull_request=proposal.original_pull_request,
            )
        )
        return created

    @staticmethod
    def _format_pr_comment(agent_name: str, result: AgentResult) -> str:
        """Render a human-friendly PR comment summarizing an agent's outcome."""
        approval = result.artifacts.get("decision", {}).get("requires_approval", True)
        return (
            "## Evolutionary CI/CD Agent\n\n"
            f"### {agent_name.replace('_', ' ').title()}\n\n"
            f"{result.message}\n\n"
            f"Requires human approval: {'Yes' if approval else 'No'}\n"
        )
