"""Measure agent outcomes without equating merged PRs with verified success."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from statistics import mean
from typing import Any

from agents.base import AgentContext, AgentDecision, BaseAgent
from agents.issue_proposals import IssueProposal
from telemetry.models import KNOWN_AGENT_NAMES


class AgentEffectivenessAgent(BaseAgent):
    """Calculate issue funnel metrics from explicit lifecycle telemetry events."""

    name = "agent_effectiveness_agent"
    evaluation_window_days = 90

    @staticmethod
    def calculate_metrics(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        cutoff = datetime.now(UTC) - timedelta(days=AgentEffectivenessAgent.evaluation_window_days)
        for record in records:
            agent = record.get("agent")
            if (
                isinstance(agent, str)
                and agent in KNOWN_AGENT_NAMES
                and agent != AgentEffectivenessAgent.name
            ):
                if record.get("dry_run") is True:
                    continue
                raw_timestamp = record.get("timestamp") or record.get("issue_created_at")
                if isinstance(raw_timestamp, str):
                    try:
                        timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
                        if timestamp.tzinfo is None:
                            timestamp = timestamp.replace(tzinfo=UTC)
                        if timestamp < cutoff:
                            continue
                    except ValueError:
                        pass
                grouped[agent].append(record)
        metrics: dict[str, dict[str, Any]] = {}
        for agent, events in grouped.items():
            counts = Counter(str(event.get("event", "")) for event in events)

            def identifier(event: dict[str, Any], field: str) -> int | str | None:
                value = event.get(field)
                return value if isinstance(value, int | str) else None

            create_events = [event for event in events if event.get("event") == "issue_created"]
            created = sum(event.get("success") is not False for event in create_events)
            creation_failures = len(create_events) - created
            reused = counts["issue_reused"]
            attempts = created + creation_failures + reused + counts["issue_proposed"]
            linked_issues = {
                identifier(event, "issue_number")
                for event in events
                if event.get("event") == "issue_linked_pr"
                and identifier(event, "issue_number") is not None
            }
            linked_prs = {
                identifier(event, "associated_pull_request") or identifier(event, "issue_number")
                for event in events
                if event.get("event") == "issue_linked_pr"
                and (
                    identifier(event, "associated_pull_request") is not None
                    or identifier(event, "issue_number") is not None
                )
            }
            merged_issues = {
                identifier(event, "issue_number")
                for event in events
                if event.get("event") == "pr_merged"
                and identifier(event, "issue_number") is not None
            }
            merged_prs = {
                identifier(event, "associated_pull_request") or identifier(event, "issue_number")
                for event in events
                if event.get("event") == "pr_merged"
                and (
                    identifier(event, "associated_pull_request") is not None
                    or identifier(event, "issue_number") is not None
                )
            }
            verified_issues = {
                identifier(event, "issue_number")
                for event in events
                if event.get("event") == "remediation_verified"
                and identifier(event, "issue_number") is not None
            }
            measured = [event for event in events if event.get("event") == "measured_outcome"]
            measured_targets = [
                event
                for event in measured
                if event.get("measured_outcome") in {"target_met", "target_missed"}
            ]
            issue_to_pr_hours = [
                elapsed
                for event in events
                if event.get("event") == "issue_linked_pr"
                for elapsed in [
                    AgentEffectivenessAgent._elapsed_hours(
                        event.get("issue_created_at"), event.get("pr_created_at")
                    )
                ]
                if elapsed is not None
            ]
            pr_to_merge_hours = [
                elapsed
                for event in events
                if event.get("event") == "pr_merged"
                for elapsed in [
                    AgentEffectivenessAgent._elapsed_hours(
                        event.get("pr_created_at"), event.get("pr_merged_at")
                    )
                ]
                if elapsed is not None
            ]
            validation_failures = (
                sum(
                    event.get("event") == "pr_validation_result"
                    and event.get("pr_validation_result") == "failure"
                    for event in events
                )
                + counts["pr_validation_failed"]
            )
            metrics[agent] = {
                "issues_created": created,
                "duplicates": reused,
                "issues_attempted": attempts,
                "issue_creation_failures": creation_failures,
                "issues_with_linked_pr": len(linked_issues),
                "linked_pull_requests": len(linked_prs),
                "merged_prs": len(merged_prs),
                "merged_issues": len(merged_issues),
                "verified_remediations": len(verified_issues),
                "issue_to_pr_conversion_rate": (len(linked_issues) / created if created else None),
                "pr_acceptance_rate": (len(merged_prs) / len(linked_prs) if linked_prs else None),
                "duplicate_issue_rate": reused / attempts if attempts else None,
                "verified_remediation_rate": (
                    len(verified_issues) / len(merged_issues) if merged_issues else None
                ),
                "measured_improvement_rate": (
                    sum(event.get("measured_outcome") == "target_met" for event in measured_targets)
                    / len(measured_targets)
                    if measured_targets
                    else None
                ),
                "incomplete_issues": max(created - len(linked_issues), 0),
                "pending_linked_prs": max(len(linked_prs) - len(merged_prs), 0),
                "rejected_issues": counts["issue_rejected"],
                "repeated_findings_after_remediation": (
                    AgentEffectivenessAgent._repeated_after_verification(events)
                ),
                "copilot_assignment_succeeded": counts["copilot_assignment_succeeded"],
                "copilot_assignment_failed": counts["copilot_assignment_failed"],
                "validation_failures": validation_failures,
                "average_issue_to_pr_hours": mean(issue_to_pr_hours) if issue_to_pr_hours else None,
                "average_pr_to_merge_hours": mean(pr_to_merge_hours) if pr_to_merge_hours else None,
                "sample_events": len(events),
            }
        return metrics

    @staticmethod
    def _elapsed_hours(start: Any, end: Any) -> float | None:
        if not isinstance(start, str) or not isinstance(end, str):
            return None
        try:
            start_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_time = datetime.fromisoformat(end.replace("Z", "+00:00"))
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=UTC)
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=UTC)
            return max((end_time - start_time).total_seconds() / 3600, 0.0)
        except ValueError:
            return None

    @staticmethod
    def _repeated_after_verification(events: list[dict[str, Any]]) -> int:
        verified_fingerprints: set[str] = set()
        repeats = 0
        ordered = sorted(events, key=lambda item: str(item.get("timestamp", "")))
        for event in ordered:
            fingerprint = event.get("issue_fingerprint")
            if not isinstance(fingerprint, str) or not fingerprint:
                continue
            if event.get("event") == "remediation_verified":
                verified_fingerprints.add(fingerprint)
            elif event.get("event") == "issue_created" and fingerprint in verified_fingerprints:
                repeats += 1
        return repeats

    def observe(self, context: AgentContext) -> dict[str, Any]:
        metrics = self.calculate_metrics(context.telemetry)
        findings: list[dict[str, Any]] = []
        for agent, values in sorted(metrics.items()):
            duplicate_rate = values["duplicate_issue_rate"]
            verified_rate = values["verified_remediation_rate"]
            if (
                duplicate_rate is not None
                and values["issues_attempted"] >= 5
                and duplicate_rate >= 0.4
            ):
                findings.append(
                    {
                        "agent": agent,
                        "finding_type": "high_duplicate_rate",
                        "value": duplicate_rate,
                        "evidence": (
                            f"{values['duplicates']} duplicate findings in "
                            f"{values['issues_attempted']} issue attempts"
                        ),
                    }
                )
            if verified_rate is not None and values["merged_prs"] >= 3 and verified_rate < 0.5:
                findings.append(
                    {
                        "agent": agent,
                        "finding_type": "low_verified_remediation_rate",
                        "value": verified_rate,
                        "evidence": (
                            f"{values['verified_remediations']} verified remediations after "
                            f"{values['merged_prs']} merged PRs"
                        ),
                    }
                )
        return {
            "metrics": metrics,
            "findings": findings,
            "records_analyzed": len(context.telemetry),
        }

    def reason(self, context: AgentContext, observation: dict[str, Any]) -> AgentDecision:
        findings = observation["findings"]
        if not findings:
            return AgentDecision(
                action="no_action",
                reason=(
                    f"Evaluated {observation['records_analyzed']} lifecycle telemetry records; "
                    "no sufficiently supported agent-quality pattern was found."
                ),
                arguments={"metrics": observation["metrics"]},
                confidence=0.7,
                requires_approval=False,
            )
        finding = findings[0]
        agent = finding["agent"]
        evidence = finding["evidence"]
        if finding["finding_type"] == "high_duplicate_rate":
            requested = (
                "Review the agent's fingerprinting and evidence thresholds to reduce "
                "repeated issue proposals."
            )
            title = f"[Agent Improvement] Reduce duplicate {agent.replace('_agent', '')} findings"
        else:
            requested = (
                "Review the agent's evidence criteria, acceptance checks, and follow-up telemetry. "
                "Do not treat merged PRs as verified remediations."
            )
            title = (
                f"[Agent Improvement] Improve verified outcomes for {agent.replace('_agent', '')}"
            )
        confidence = 0.82
        proposal = IssueProposal(
            title=title,
            summary=f"{agent} shows a measurable outcome-quality concern.",
            problem=(
                f"The evaluation window contains {evidence}. This is a signal for review, "
                "not proof that any individual agent action was unsuccessful."
            ),
            evidence=[
                evidence,
                "Issue-to-PR conversion: "
                f"{self._percent(observation['metrics'][agent]['issue_to_pr_conversion_rate'])}",
                "PR acceptance: "
                f"{self._percent(observation['metrics'][agent]['pr_acceptance_rate'])}",
                "Verified remediation rate: "
                f"{self._percent(observation['metrics'][agent]['verified_remediation_rate'])}",
                "Measured improvement rate: "
                f"{self._percent(observation['metrics'][agent]['measured_improvement_rate'])}",
                "Average issue-to-PR time: "
                f"{observation['metrics'][agent]['average_issue_to_pr_hours']}",
                "Average PR-to-merge time: "
                f"{observation['metrics'][agent]['average_pr_to_merge_hours']}",
                "Pending, incomplete, rejected, and unverified outcomes are kept distinct.",
            ],
            affected_files=[f"agents/{agent.removesuffix('_agent')}/agent.py"],
            requested_change=requested,
            acceptance_criteria=[
                "The suspected cause is confirmed from source and lifecycle evidence.",
                "Any prompt, deterministic threshold, or agent configuration change has "
                "regression tests.",
                "Security policies and approval controls remain unchanged without explicit "
                "human authorization.",
            ],
            validation_commands=[
                "pytest -q",
                "ruff check .",
                "mypy app agents tools llm telemetry scripts",
            ],
            labels=["agent-recommendation"],
            source_agent=self.name,
            confidence=confidence,
            deduplication_key=f"{finding['finding_type']}:{agent}",
            assign_to_copilot=False,
            finding_type=f"{finding['finding_type']}:{agent}",
        )
        return AgentDecision(
            action="report_agent_effectiveness",
            reason=proposal.summary,
            tool="create_issue",
            arguments={
                "metrics": observation["metrics"],
                "issue_proposal": proposal.model_dump(mode="json"),
            },
            confidence=confidence,
            requires_approval=True,
        )

    @staticmethod
    def _percent(value: float | None) -> str:
        return "unavailable" if value is None else f"{value:.1%}"

    def act(self, context: AgentContext, decision: AgentDecision) -> dict[str, Any]:
        return {"metrics": decision.arguments.get("metrics", {}), "published": False}

    def validate(
        self, context: AgentContext, decision: AgentDecision, tool_result: dict[str, Any]
    ) -> dict[str, Any]:
        return {"metrics_computed": isinstance(tool_result.get("metrics"), dict)}
