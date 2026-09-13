"""Merge Conflict Agent.

Detects conflicted files, reads conflict markers and surrounding code/tests,
proposes a resolution, and validates that resolution by running tests. It
never merges the PR itself, and any proposed modification requires approval
per policy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.base import AgentContext, AgentDecision, BaseAgent
from tools.git.operations import ConflictBlock, extract_conflict_blocks, find_conflicted_files
from tools.testing.pytest_tools import run_pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class MergeConflictAgent(BaseAgent):
    """Proposes and validates resolutions for merge conflicts."""

    name = "merge_conflict_agent"

    def observe(self, context: AgentContext) -> dict[str, Any]:
        conflicted_files = find_conflicted_files(REPO_ROOT)
        blocks_by_file: dict[str, list[ConflictBlock]] = {}
        for file_name in conflicted_files:
            file_path = REPO_ROOT / file_name
            if file_path.exists():
                blocks_by_file[file_name] = extract_conflict_blocks(file_path)
        return {"conflicted_files": conflicted_files, "blocks_by_file": blocks_by_file}

    def reason(self, context: AgentContext, observation: dict[str, Any]) -> AgentDecision:
        conflicted_files = observation.get("conflicted_files", [])
        if not conflicted_files:
            fallback = AgentDecision(
                action="no_action",
                reason="No conflicted files detected.",
                confidence=0.3,
                requires_approval=False,
            )
        else:
            fallback = AgentDecision(
                action="propose_resolution",
                reason=(
                    f"Detected conflicts in {len(conflicted_files)} file(s): "
                    f"{', '.join(conflicted_files)}. Proposing a resolution that keeps "
                    "both changes where possible; validating with the test suite."
                ),
                tool="execute_tests",
                arguments={"files": conflicted_files},
                confidence=0.5,
                requires_approval=True,
            )
        return self.reason_with_llm(
            context, observation, fallback, "merge_conflict.md", ["execute_tests"]
        )

    def act(self, context: AgentContext, decision: AgentDecision) -> dict[str, Any]:
        if decision.tool != "execute_tests":
            return {}
        run_result = run_pytest(["tests"], REPO_ROOT)
        return {
            "passed": run_result.passed,
            "failed": run_result.failed,
            "all_passed": run_result.all_passed,
        }

    def validate(
        self, context: AgentContext, decision: AgentDecision, tool_result: dict[str, Any]
    ) -> dict[str, Any]:
        if decision.tool != "execute_tests":
            return {"skipped": True}
        return {
            "resolution_validated": tool_result.get("all_passed", False),
            "passed": tool_result.get("passed", 0),
            "failed": tool_result.get("failed", 0),
        }
