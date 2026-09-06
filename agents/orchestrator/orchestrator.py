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
from telemetry.logger import get_logger
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

        result = agent.run(context, policy_check=policy_check)

        if context.pull_request_number is not None:
            comment = self._format_pr_comment(agent.name, result)
            self.github_client.post_pr_comment(context.pull_request_number, comment)
        return result


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
