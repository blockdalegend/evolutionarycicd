"""Pipeline Optimizer Agent -- the "Evolutionary CI/CD" agent.

Analyzes historical pipeline telemetry (see ``data/pipeline_history.json``)
to identify systemic issues -- flaky tests, repeatedly failing stages, slow
stages, and recurring dependency failures -- and produces recommendations.

It never rewrites production workflow files itself. Its output is always a
recommendation (optionally with a proposed patch) that requires human
approval, matching ``policies/agent_permissions.yml`` where
``modify_workflow`` is ``false``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean
from typing import Any

from agents.base import AgentContext, AgentDecision, BaseAgent

#: A stage is considered "repeatedly failing" once it fails at least this
#: many times across the observed history.
REPEATED_FAILURE_THRESHOLD = 3

#: A run is considered "slow" once its duration exceeds this multiple of the
#: mean duration for its workflow.
SLOW_RUN_MULTIPLIER = 1.5

#: A failure reason is considered "flaky" once it recurs at least this often
#: while also having at least one successful run of the same workflow.
FLAKY_THRESHOLD = 2


class PipelineOptimizerAgent(BaseAgent):
    """Recommends delivery-pipeline improvements based on historical telemetry."""

    name = "pipeline_optimizer_agent"

    def observe(self, context: AgentContext) -> dict[str, Any]:
        return {"runs": context.pipeline_history}

    def reason(self, context: AgentContext, observation: dict[str, Any]) -> AgentDecision:
        runs = observation.get("runs", [])
        if not runs:
            fallback = AgentDecision(
                action="no_action",
                reason="No pipeline history available to analyze.",
                confidence=0.2,
                requires_approval=False,
            )
        else:
            recommendations = self._analyze(runs)
            if not recommendations:
                fallback = AgentDecision(
                    action="no_action",
                    reason=f"Analyzed {len(runs)} historical runs; no systemic issues found.",
                    confidence=0.5,
                    requires_approval=False,
                )
            else:
                fallback = AgentDecision(
                    action="recommend_pipeline_improvements",
                    reason=(
                        f"Analyzed {len(runs)} historical runs and identified "
                        f"{len(recommendations)} recommendation(s):\n"
                        + "\n".join(f"- {rec}" for rec in recommendations)
                    ),
                    tool="propose_workflow_change",
                    arguments={"recommendations": recommendations},
                    confidence=0.65,
                    requires_approval=True,
                )
        return self.reason_with_llm(
            context,
            observation,
            fallback,
            "pipeline_optimizer.md",
            ["propose_workflow_change"],
        )

    def act(self, context: AgentContext, decision: AgentDecision) -> dict[str, Any]:
        # This agent only *proposes*; it never modifies workflow files. The
        # caller (scripts/run_agent.py) is responsible for turning the
        # recommendation into a GitHub issue for human review.
        return {"recommendations": decision.arguments.get("recommendations", [])}

    def validate(
        self, context: AgentContext, decision: AgentDecision, tool_result: dict[str, Any]
    ) -> dict[str, Any]:
        return {"recommendation_count": len(tool_result.get("recommendations", []))}

    @staticmethod
    def _analyze(runs: list[dict[str, Any]]) -> list[str]:
        """Return human-readable recommendations derived from ``runs``."""
        recommendations: list[str] = []

        failing_stage_counts: Counter[str] = Counter()
        durations_by_workflow: dict[str, list[float]] = defaultdict(list)
        failure_reason_counts: Counter[str] = Counter()
        retry_counts_by_workflow: dict[str, list[int]] = defaultdict(list)

        for run in runs:
            workflow = run.get("workflow", "unknown")
            durations_by_workflow[workflow].append(float(run.get("duration_seconds", 0)))
            retry_counts_by_workflow[workflow].append(int(run.get("retry_count", 0)))
            if run.get("status") == "failure":
                stage = run.get("failed_stage")
                if stage:
                    failing_stage_counts[f"{workflow}:{stage}"] += 1
                reason = run.get("failure_reason")
                if reason:
                    failure_reason_counts[reason] += 1

        for stage_key, count in failing_stage_counts.items():
            if count >= REPEATED_FAILURE_THRESHOLD:
                workflow, _, stage = stage_key.partition(":")
                recommendations.append(
                    f"Stage '{stage}' in workflow '{workflow}' failed {count} times "
                    "historically -- investigate and consider quarantining or fixing it."
                )

        for reason, count in failure_reason_counts.items():
            if count >= FLAKY_THRESHOLD and "flaky" in reason.lower():
                recommendations.append(
                    f"Recurring flaky-test signal '{reason}' seen {count} times -- "
                    "consider quarantining the flaky test."
                )
            elif count >= FLAKY_THRESHOLD and "dependency" in reason.lower():
                recommendations.append(
                    f"Recurring dependency failure '{reason}' seen {count} times -- "
                    "investigate the upstream dependency."
                )

        for workflow, durations in durations_by_workflow.items():
            if len(durations) < 2:
                continue
            avg = mean(durations)
            slow_runs = [d for d in durations if d > avg * SLOW_RUN_MULTIPLIER]
            if slow_runs:
                recommendations.append(
                    f"Workflow '{workflow}' has {len(slow_runs)} run(s) significantly "
                    f"slower than its {avg:.0f}s average -- consider caching dependencies "
                    "or splitting slow test suites."
                )

        for workflow, retries in retry_counts_by_workflow.items():
            if retries and mean(retries) >= 1.0:
                recommendations.append(
                    f"Workflow '{workflow}' averages {mean(retries):.1f} retries per run -- "
                    "consider a workflow condition change to fail faster or reduce retries."
                )

        return recommendations
