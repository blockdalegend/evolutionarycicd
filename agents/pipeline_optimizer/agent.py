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

import json
from collections import Counter, defaultdict
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel, Field

from agents.base import PROMPTS_DIR, AgentContext, AgentDecision, BaseAgent
from llm.client import LLMClient
from llm.models import LLMMessage, LLMRequest
from telemetry.logger import get_logger

#: A stage is considered "repeatedly failing" once it fails at least this
#: many times across the observed history.
REPEATED_FAILURE_THRESHOLD = 3

#: A run is considered "slow" once its duration exceeds this multiple of the
#: mean duration for its workflow.
SLOW_RUN_MULTIPLIER = 1.5

#: A failure reason is considered "flaky" once it recurs at least this often
#: while also having at least one successful run of the same workflow.
FLAKY_THRESHOLD = 2
logger = get_logger(__name__)


class PipelineFinding(BaseModel):
    """One evidence-backed finding and its model-authored remediation."""

    category: Literal["repeated_failure", "flaky_test", "dependency", "performance", "retries"]
    evidence: str = Field(min_length=20)
    analysis: str = Field(min_length=30)
    recommended_fix: str = Field(min_length=30)


class PipelineAnalysis(BaseModel):
    """Structured analysis returned by the pipeline optimizer LLM."""

    summary: str = Field(min_length=40)
    findings: list[PipelineFinding] = Field(min_length=1, max_length=8)


class PipelineOptimizerAgent(BaseAgent):
    """Recommends delivery-pipeline improvements based on historical telemetry."""

    name = "pipeline_optimizer_agent"

    def observe(self, context: AgentContext) -> dict[str, Any]:
        return {"runs": context.pipeline_history}

    def reason(self, context: AgentContext, observation: dict[str, Any]) -> AgentDecision:
        runs = observation.get("runs", [])
        recommendations: list[str] = []
        if not runs:
            fallback = AgentDecision(
                action="no_action",
                reason="No pipeline history available to analyze.",
                confidence=0.2,
                requires_approval=False,
            )
        else:
            recommendation_details = self._analyze_details(runs)
            recommendations = [item["recommendation"] for item in recommendation_details]
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
                    arguments={
                        "recommendations": recommendations,
                        "recommended_fixes": recommendation_details,
                    },
                    confidence=0.65,
                    requires_approval=True,
                )
        if not runs:
            return fallback
        analysis = self._request_llm_analysis(context, observation)
        if analysis is None:
            return fallback
        return self._decision_from_analysis(analysis)

    @classmethod
    def _decision_from_analysis(cls, analysis: dict[str, Any]) -> AgentDecision:
        findings = analysis["findings"]
        recommendations = [finding["analysis"] for finding in findings]
        recommended_fixes = [
            {
                "recommendation": finding["analysis"],
                "recommended_fix": finding["recommended_fix"],
                "evidence": finding["evidence"],
                "category": finding["category"],
            }
            for finding in findings
        ]
        reason = analysis["summary"] + "\n\n" + "\n".join(
            f"- {finding['analysis']}\n  Fix: {finding['recommended_fix']}"
            for finding in findings
        )
        return AgentDecision(
            action="recommend_pipeline_improvements",
            reason=reason,
            tool="propose_workflow_change",
            arguments={
                "analysis_source": "llm",
                "llm_analysis": analysis,
                "recommendations": recommendations,
                "recommended_fixes": recommended_fixes,
            },
            confidence=0.8,
            requires_approval=True,
        )

    @classmethod
    def _request_llm_analysis(
        cls, context: AgentContext, observation: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Request and validate an evidence-backed analysis from the configured LLM."""
        prompt_path = PROMPTS_DIR / "pipeline_optimizer.md"
        try:
            system_prompt = prompt_path.read_text(encoding="utf-8")
            request = LLMRequest(
                messages=[
                    LLMMessage(
                        role="system",
                        content=(
                            system_prompt
                            + "\n\nReturn only a JSON PipelineAnalysis object matching the schema. "
                            "Do not return an AgentDecision. Every finding must cite exact "
                            "workflow, stage, reason, count, or duration evidence from the "
                            "supplied runs. Explain why the pattern matters and propose a "
                            "specific fix. Never use generic phrases such as 'investigate', "
                            "'improve the pipeline', or 'add more tests' without naming the "
                            "workflow/stage and the concrete change."
                        ),
                    ),
                    LLMMessage(
                        role="user",
                        content=(
                            "Analyze these historical pipeline runs as an expert CI/CD "
                            "engineer. Produce only evidence-backed findings.\n\n"
                            f"Agent context:\n{context.model_dump_json()}\n\n"
                            f"Observation:\n{json.dumps(observation, default=str)}"
                        ),
                    ),
                ],
                response_schema=PipelineAnalysis.model_json_schema(),
                temperature=0.2,
                max_tokens=4000,
            )
            response = LLMClient().complete(request)
            parsed = response.parsed
            if parsed is None and response.content:
                content = response.content.strip()
                parsed = json.loads(content[content.find("{") : content.rfind("}") + 1])
            if not isinstance(parsed, dict):
                raise ValueError("LLM did not return a PipelineAnalysis object")
            analysis = PipelineAnalysis.model_validate(parsed).model_dump()
            evidence = json.dumps(observation, default=str)
            for finding in analysis["findings"]:
                if not any(
                    token in evidence
                    for token in finding["evidence"].split()
                    if len(token) >= 4
                ):
                    raise ValueError("LLM finding does not cite supplied telemetry")
            return analysis
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Pipeline optimizer LLM analysis unavailable: %s", exc)
            return None

    def act(self, context: AgentContext, decision: AgentDecision) -> dict[str, Any]:
        # This agent only *proposes*; it never modifies workflow files. The
        # caller (scripts/run_agent.py) is responsible for turning the
        # recommendation into a GitHub issue for human review.
        return {
            "recommendations": decision.arguments.get("recommendations", []),
            "recommended_fixes": decision.arguments.get("recommended_fixes", []),
        }

    def validate(
        self, context: AgentContext, decision: AgentDecision, tool_result: dict[str, Any]
    ) -> dict[str, Any]:
        return {"recommendation_count": len(tool_result.get("recommendations", []))}

    @staticmethod
    def _analyze(runs: list[dict[str, Any]]) -> list[str]:
        """Return human-readable recommendations derived from ``runs``."""
        return [item["recommendation"] for item in PipelineOptimizerAgent._analyze_details(runs)]

    @staticmethod
    def _analyze_details(runs: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Return recommendations with a concrete, reviewable fix for each issue."""
        recommendations: list[str] = []
        details: list[dict[str, str]] = []

        def add(recommendation: str, recommended_fix: str) -> None:
            recommendations.append(recommendation)
            details.append(
                {
                    "recommendation": recommendation,
                    "recommended_fix": recommended_fix,
                }
            )

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
                add(
                    f"Stage '{stage}' in workflow '{workflow}' failed {count} times "
                    "historically -- investigate and consider quarantining or fixing it.",
                    f"Inspect the '{stage}' logs, reproduce the failure, and repair the "
                    "underlying check. Quarantine the stage only if the failure is confirmed "
                    "to be nondeterministic, with an owner and exit criteria.",
                )

        for reason, count in failure_reason_counts.items():
            if count >= FLAKY_THRESHOLD and "flaky" in reason.lower():
                add(
                    f"Recurring flaky-test signal '{reason}' seen {count} times -- "
                    "consider quarantining the flaky test.",
                    f"Quarantine '{reason}' behind an explicit tracking issue, capture the "
                    "failure evidence, and replace external timing dependence with a "
                    "deterministic fixture or bounded retry before re-enabling it.",
                )
            elif count >= FLAKY_THRESHOLD and "dependency" in reason.lower():
                add(
                    f"Recurring dependency failure '{reason}' seen {count} times -- "
                    "investigate the upstream dependency.",
                    "Add dependency caching and a bounded retry for transient registry errors; "
                    "pin or mirror the affected dependency and alert on repeated failures.",
                )

        for workflow, durations in durations_by_workflow.items():
            if len(durations) < 2:
                continue
            avg = mean(durations)
            slow_runs = [d for d in durations if d > avg * SLOW_RUN_MULTIPLIER]
            if slow_runs:
                add(
                    f"Workflow '{workflow}' has {len(slow_runs)} run(s) significantly "
                    f"slower than its {avg:.0f}s average -- consider caching dependencies "
                    "or splitting slow test suites.",
                    f"Profile the slow '{workflow}' runs, cache dependency installation, and "
                    "split the slowest test group into parallel jobs with separate timing "
                    "budgets.",
                )

        for workflow, retries in retry_counts_by_workflow.items():
            if retries and mean(retries) >= 1.0:
                add(
                    f"Workflow '{workflow}' averages {mean(retries):.1f} retries per run -- "
                    "consider a workflow condition change to fail faster or reduce retries.",
                    f"Review retry conditions in '{workflow}', retry only known transient "
                    "failures, cap attempts, and fail fast for deterministic test or lint "
                    "errors.",
                )

        return details
