"""Detect meaningful CI duration regressions from GitHub run/job timings."""

from __future__ import annotations

import os
from collections import defaultdict
from statistics import mean
from typing import Any

from agents.base import AgentContext, AgentDecision, BaseAgent
from agents.issue_proposals import IssueProposal

MIN_SAMPLES_PER_WINDOW = 3
RECENT_WINDOW = 5
DEFAULT_REGRESSION_THRESHOLD = 0.3


class BuildPerformanceAgent(BaseAgent):
    """Compare recorded workflow/job durations without inventing stage metrics."""

    name = "build_performance_agent"

    def observe(self, context: AgentContext) -> dict[str, Any]:
        try:
            threshold = float(os.environ.get("AGENT_BUILD_REGRESSION_THRESHOLD", "0.3"))
            if not 0 < threshold < 10:
                threshold = DEFAULT_REGRESSION_THRESHOLD
        except ValueError:
            threshold = DEFAULT_REGRESSION_THRESHOLD
        by_workflow: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for run in context.build_history or context.pipeline_history:
            duration = run.get("duration_seconds")
            if isinstance(duration, int | float) and duration > 0:
                by_workflow[str(run.get("workflow", "unknown"))].append(run)
        regressions: list[dict[str, Any]] = []
        for workflow, runs in sorted(by_workflow.items()):
            runs.sort(key=lambda item: str(item.get("timestamp", item.get("run_id", ""))))
            recent_count = min(RECENT_WINDOW, len(runs) // 2)
            baseline_runs = runs[:-recent_count] if recent_count else []
            recent_runs = runs[-recent_count:] if recent_count else []
            if min(len(baseline_runs), len(recent_runs)) < MIN_SAMPLES_PER_WINDOW:
                continue
            baseline = mean(float(run["duration_seconds"]) for run in baseline_runs)
            recent = mean(float(run["duration_seconds"]) for run in recent_runs)
            regression = (recent - baseline) / baseline if baseline else 0.0
            if regression < threshold:
                continue
            job_regressions = self._job_regressions(baseline_runs, recent_runs, threshold)
            stage_regressions = self._stage_regressions(baseline_runs, recent_runs)
            baseline_artifacts = [
                float(run["artifact_bytes"])
                for run in baseline_runs
                if isinstance(run.get("artifact_bytes"), int | float)
            ]
            recent_artifacts = [
                float(run["artifact_bytes"])
                for run in recent_runs
                if isinstance(run.get("artifact_bytes"), int | float)
            ]
            regressions.append(
                {
                    "workflow": workflow,
                    "workflow_path": next(
                        (
                            str(run["workflow_path"])
                            for run in recent_runs
                            if run.get("workflow_path")
                        ),
                        "",
                    ),
                    "baseline_seconds": round(baseline, 2),
                    "recent_seconds": round(recent, 2),
                    "regression_rate": regression,
                    "baseline_samples": len(baseline_runs),
                    "recent_samples": len(recent_runs),
                    "job_regressions": job_regressions,
                    "baseline_artifact_bytes": (
                        round(mean(baseline_artifacts), 2) if baseline_artifacts else None
                    ),
                    "recent_artifact_bytes": (
                        round(mean(recent_artifacts), 2) if recent_artifacts else None
                    ),
                    "stage_regressions": stage_regressions,
                    "cache_hit_rates": [
                        run["cache_hit_rate"]
                        for run in recent_runs
                        if isinstance(run.get("cache_hit_rate"), int | float)
                    ],
                }
            )
        return {"regressions": regressions, "threshold": threshold}

    @staticmethod
    def _job_regressions(
        baseline_runs: list[dict[str, Any]],
        recent_runs: list[dict[str, Any]],
        threshold: float,
    ) -> list[dict[str, Any]]:
        def grouped(runs: list[dict[str, Any]]) -> dict[str, list[float]]:
            output: dict[str, list[float]] = defaultdict(list)
            for run in runs:
                for job in run.get("jobs", []):
                    value = job.get("duration_seconds")
                    if isinstance(value, int | float) and value > 0:
                        output[str(job.get("name", "unknown"))].append(float(value))
            return output

        baseline, recent = grouped(baseline_runs), grouped(recent_runs)
        result: list[dict[str, Any]] = []
        for name in sorted(baseline.keys() & recent.keys()):
            before, after = mean(baseline[name]), mean(recent[name])
            if before and (after - before) / before >= threshold:
                result.append(
                    {
                        "job": name,
                        "baseline_seconds": round(before, 2),
                        "recent_seconds": round(after, 2),
                    }
                )
        return result

    @staticmethod
    def _stage_regressions(
        baseline_runs: list[dict[str, Any]], recent_runs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        def grouped(runs: list[dict[str, Any]]) -> dict[str, list[float]]:
            output: dict[str, list[float]] = defaultdict(list)
            for run in runs:
                stages = run.get("stage_durations", {})
                if not isinstance(stages, dict):
                    continue
                for name, duration in stages.items():
                    if isinstance(duration, int | float) and duration > 0:
                        output[str(name)].append(float(duration))
            return output

        baseline, recent = grouped(baseline_runs), grouped(recent_runs)
        return [
            {
                "stage": name,
                "baseline_seconds": round(mean(baseline[name]), 2),
                "recent_seconds": round(mean(recent[name]), 2),
            }
            for name in sorted(baseline.keys() & recent.keys())
        ]

    def reason(self, context: AgentContext, observation: dict[str, Any]) -> AgentDecision:
        regressions = observation["regressions"]
        if not regressions:
            return AgentDecision(
                action="no_action",
                reason=(
                    "No workflow duration regression exceeded the configured threshold "
                    "with enough samples."
                ),
                confidence=0.5,
                requires_approval=False,
            )
        finding = max(regressions, key=lambda item: item["regression_rate"])
        percent = finding["regression_rate"] * 100
        workflow = finding["workflow"]
        reason = (
            f"{workflow} average duration regressed by {percent:.1f}% "
            "against its historical baseline."
        )
        job_lines = [
            f"Job {job['job']}: {job['baseline_seconds']:.1f}s -> {job['recent_seconds']:.1f}s"
            for job in finding["job_regressions"]
        ]
        if not job_lines:
            job_lines = ["No comparable per-job regression was present in the supplied API data."]
        job_lines.extend(
            f"Reported stage {stage['stage']}: {stage['baseline_seconds']:.1f}s -> "
            f"{stage['recent_seconds']:.1f}s"
            for stage in finding["stage_regressions"]
        )
        job_lines.extend(
            f"Recent cache hit rate: {rate:.1%}" for rate in finding["cache_hit_rates"]
        )
        if (
            finding["baseline_artifact_bytes"] is not None
            and finding["recent_artifact_bytes"] is not None
        ):
            job_lines.append(
                "Average artifact size: "
                f"{finding['baseline_artifact_bytes']:.0f} bytes -> "
                f"{finding['recent_artifact_bytes']:.0f} bytes"
            )
        workflow_path = finding["workflow_path"]
        files = [workflow_path] if workflow_path else []
        confidence = min(0.95, 0.75 + 0.02 * min(finding["recent_samples"], 5))
        proposal = IssueProposal(
            title=f"[Build Performance] Investigate {percent:.1f}% build-duration regression",
            summary=reason,
            problem=(
                f"Workflow `{workflow}` slowed from {finding['baseline_seconds']:.1f}s "
                f"(baseline) to {finding['recent_seconds']:.1f}s (recent average)."
            ),
            evidence=[
                f"Baseline: {finding['baseline_seconds']:.1f}s across "
                f"{finding['baseline_samples']} runs",
                f"Recent: {finding['recent_seconds']:.1f}s across "
                f"{finding['recent_samples']} runs",
                f"Regression: {percent:.1f}% (threshold {observation['threshold']:.0%})",
                *job_lines,
                "Dependency-install, build, test-stage, cache-hit, and artifact-size "
                "attribution is unavailable unless explicitly present in run/job evidence.",
            ],
            affected_files=files,
            requested_change=(
                "Inspect the measured slow job and its workflow configuration, then make a "
                "reviewable optimization such as effective caching or removal of redundant work. "
                "Do not modify workflow execution policy without human review."
            ),
            acceptance_criteria=[
                "The affected workflow/job is measured before and after the change "
                "using comparable runs.",
                "The regression is reduced below the configured threshold without "
                "reducing required validation.",
                "Workflow changes are reviewed and merged by a human.",
            ],
            validation_commands=[
                "pytest -q",
                "ruff check .",
                "mypy app agents tools llm telemetry scripts",
            ],
            labels=["agent-recommendation"],
            source_agent=self.name,
            confidence=confidence,
            deduplication_key=f"workflow:{workflow}",
            assign_to_copilot=False,
            finding_type=f"duration_regression:{workflow}",
        )
        return AgentDecision(
            action="report_build_regression",
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
        return {"measurement_supported": bool(tool_result.get("finding"))}
