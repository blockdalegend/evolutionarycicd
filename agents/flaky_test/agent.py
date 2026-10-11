"""Detect intermittent pytest failures from historical JUnit outcomes."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from agents.base import AgentContext, AgentDecision, BaseAgent
from agents.issue_proposals import IssueProposal

MIN_RUNS = 5
MIN_FAILURES = 2
MAX_INTERMITTENT_RATE = 0.8
_CAUSE_HINTS = re.compile(
    r"timeout|timing|sleep|race|connection|network|temporary|resource busy", re.I
)
_TEST_IDENTIFIER = re.compile(r"[^A-Za-z0-9_.:/-]")
_TEST_PATH = re.compile(r"[^A-Za-z0-9_./-]")


def _stable_test_id(value: object) -> str:
    return _TEST_IDENTIFIER.sub("_", str(value)).strip("_")[:300]


def _safe_test_path(value: str) -> str | None:
    path = Path(value)
    if not value.startswith("tests/") or ".." in path.parts:
        return None
    return _TEST_PATH.sub("_", value)[:300]


class FlakyTestAgent(BaseAgent):
    """Find tests that fail across runs but also pass, without quarantining them."""

    name = "flaky_test_agent"

    def observe(self, context: AgentContext) -> dict[str, Any]:
        outcomes: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"runs": set(), "failed_runs": set(), "reruns_passed": 0, "details": []}
        )
        for run in context.test_history:
            run_id = str(run.get("run_id", ""))
            cases = run.get("cases", [])
            if not isinstance(cases, list):
                continue
            for case in cases:
                if not isinstance(case, dict):
                    continue
                test_id = _stable_test_id(case.get("test_id", ""))
                if not test_id:
                    continue
                item = outcomes[test_id]
                item["runs"].add(run_id or f"run-{len(item['runs'])}")
                if case.get("outcome") == "failed":
                    item["failed_runs"].add(run_id or f"run-{len(item['runs'])}")
                    detail = str(case.get("failure_detail", ""))
                    if detail:
                        item["details"].extend(
                            match.group(0).lower() for match in _CAUSE_HINTS.finditer(detail)
                        )
                if case.get("successful_rerun") is True:
                    item["reruns_passed"] += 1
        candidates: list[dict[str, Any]] = []
        persistent: list[dict[str, Any]] = []
        for test_id, item in sorted(outcomes.items()):
            run_count = len(item["runs"])
            failure_count = len(item["failed_runs"])
            rate = failure_count / run_count if run_count else 0.0
            if run_count < MIN_RUNS or failure_count < MIN_FAILURES:
                continue
            if rate >= MAX_INTERMITTENT_RATE:
                persistent.append(
                    {
                        "test_id": test_id,
                        "runs_analyzed": run_count,
                        "failure_runs": failure_count,
                        "failure_rate": rate,
                    }
                )
                continue
            if rate <= 0:
                continue
            candidates.append(
                {
                    "test_id": test_id,
                    "runs_analyzed": run_count,
                    "failure_runs": failure_count,
                    "failure_rate": rate,
                    "successful_reruns": item["reruns_passed"],
                    "repeated_failure_hints": dict(Counter(item["details"])),
                }
            )
        return {
            "candidates": candidates,
            "persistent_failures": persistent,
            "runs_available": len(context.test_history),
        }

    def reason(self, context: AgentContext, observation: dict[str, Any]) -> AgentDecision:
        candidates = observation["candidates"]
        if not candidates:
            return AgentDecision(
                action="no_action",
                reason=(
                    "No intermittent test finding met the evidence threshold. "
                    f"{len(observation['persistent_failures'])} persistent failure(s) "
                    "remain for ordinary failure analysis."
                ),
                confidence=0.5,
                requires_approval=False,
            )
        finding = max(candidates, key=lambda item: (item["failure_rate"], item["failure_runs"]))
        test_id = finding["test_id"]
        test_name = test_id.rsplit("::", 1)[-1]
        hints = finding["repeated_failure_hints"]
        hypotheses = [f"{hint} ({count} occurrences)" for hint, count in sorted(hints.items())]
        suspected = (
            ", ".join(hypotheses) if hypotheses else "none supported by the captured failure text"
        )
        relevant_files = [
            safe_path
            for path, source in context.test_sources.items()
            if test_name in source
            for safe_path in [_safe_test_path(path)]
            if safe_path is not None
        ][:5]
        if not relevant_files:
            relevant_files = [
                safe_path
                for path in context.changed_files
                for safe_path in [_safe_test_path(path)]
                if safe_path is not None
            ][:5]
        percent = finding["failure_rate"] * 100
        reason = (
            f"{test_name} failed in {finding['failure_runs']} of "
            f"{finding['runs_analyzed']} CI runs ({percent:.1f}%)."
        )
        confidence = min(
            0.95, 0.75 + 0.01 * finding["runs_analyzed"] + 0.01 * finding["failure_runs"]
        )
        proposal = IssueProposal(
            title=f"[Flaky Test] Stabilize {test_name}",
            summary=reason,
            problem=(
                f"The stable test identifier `{test_id}` failed in {finding['failure_runs']} "
                f"of {finding['runs_analyzed']} observed runs, while also passing in others. "
                "This intermittent result makes CI feedback unreliable."
            ),
            evidence=[
                f"Runs analyzed: {finding['runs_analyzed']}",
                f"Failure runs: {finding['failure_runs']}",
                f"Failure frequency: {percent:.1f}%",
                f"Successful reruns observed: {finding['successful_reruns']}",
                f"Suspected causes (hypotheses only): {suspected}",
            ],
            affected_files=relevant_files,
            requested_change=(
                "Investigate the captured failures and replace timing, environment, or "
                "external-service dependence with deterministic synchronization or fixtures. "
                "Do not quarantine or disable the test."
            ),
            acceptance_criteria=[
                "The test passes repeatedly without retries masking failures.",
                "Any external timing or environment dependency is controlled by a "
                "deterministic fixture.",
                "No test is disabled or quarantined.",
            ],
            validation_commands=["pytest -q", "pytest -q --count=20"],
            labels=["agent-recommendation"],
            source_agent=self.name,
            confidence=confidence,
            deduplication_key=f"test:{test_id}",
            assign_to_copilot=True,
            finding_type=f"intermittent_test:{test_id}",
        )
        return AgentDecision(
            action="report_flaky_test",
            reason=reason,
            tool="create_issue",
            arguments={"finding": finding, "issue_proposal": proposal.model_dump(mode="json")},
            confidence=confidence,
            requires_approval=True,
        )

    def act(self, context: AgentContext, decision: AgentDecision) -> dict[str, Any]:
        return {"finding": decision.arguments.get("finding"), "published": False}

    def validate(
        self, context: AgentContext, decision: AgentDecision, tool_result: dict[str, Any]
    ) -> dict[str, Any]:
        return {"finding_supported": bool(tool_result.get("finding")), "test_disabled": False}
