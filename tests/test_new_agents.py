from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

from agents.agent_effectiveness.agent import AgentEffectivenessAgent
from agents.base import AgentContext
from agents.build_performance.agent import BuildPerformanceAgent
from agents.documentation_drift.agent import DocumentationDriftAgent
from agents.flaky_test.agent import FlakyTestAgent
from agents.issue_proposals import IssueProposal, proposal_from_decision
from agents.orchestrator.orchestrator import AgentOrchestrator
from agents.test_impact.agent import TestImpactAgent
from scripts.collect_pipeline_context import _collect_github_history, _parse_junit_history
from tools.github.client import GitHubClient

FIXTURES = Path(__file__).parent / "fixtures" / "agent_demos"


def _fixture(name: str) -> AgentContext:
    return AgentContext.model_validate(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def _proposal(agent: Any, context: AgentContext) -> IssueProposal | None:
    observation = agent.observe(context)
    decision = agent.reason(context, observation)
    return proposal_from_decision(agent.name, context, observation, decision)


def test_all_new_agents_initialize_with_unique_policy_names() -> None:
    agents = [
        FlakyTestAgent(),
        BuildPerformanceAgent(),
        TestImpactAgent(),
        DocumentationDriftAgent(),
        AgentEffectivenessAgent(),
    ]

    assert [agent.name for agent in agents] == [
        "flaky_test_agent",
        "build_performance_agent",
        "test_impact_agent",
        "documentation_drift_agent",
        "agent_effectiveness_agent",
    ]


def test_flaky_agent_calculates_frequency_and_retry_successes() -> None:
    agent = FlakyTestAgent()
    context = _fixture("flaky_test.json")
    context.test_history[0]["cases"][0]["failure_detail"] = "Timeout; password=secret-value"
    observation = agent.observe(context)
    finding = observation["candidates"][0]

    assert finding["runs_analyzed"] == 10
    assert finding["failure_runs"] == 3
    assert finding["failure_rate"] == 0.3
    assert finding["successful_reruns"] == 2
    proposal = _proposal(agent, context)
    assert proposal is not None
    assert proposal.title == "[Flaky Test] Stabilize test_payment_timeout"
    assert any("hypotheses" in item for item in proposal.evidence)
    assert not any("secret-value" in item for item in proposal.evidence)
    assert "No test is disabled or quarantined." in proposal.acceptance_criteria


def test_flaky_agent_excludes_persistent_and_insufficient_history() -> None:
    context = AgentContext(
        test_history=[
            {
                "run_id": str(index),
                "cases": [{"test_id": "suite::test_persistent", "outcome": "failed"}],
            }
            for index in range(5)
        ]
    )
    observation = FlakyTestAgent().observe(context)
    assert observation["candidates"] == []
    assert observation["persistent_failures"][0]["failure_rate"] == 1.0
    assert _proposal(FlakyTestAgent(), AgentContext()) is None
    assert _parse_junit_history(b"<not-xml", "bad") == {}
    entity_xml = b'<!DOCTYPE root [<!ENTITY x "unexpected">]><testsuites>&x;</testsuites>'
    assert _parse_junit_history(entity_xml, "unsafe") == {}


def test_junit_parser_marks_only_passes_after_a_failure_as_reruns() -> None:
    report = (
        b'<testsuite><testcase classname="suite" name="test_x" />'
        b'<testcase classname="suite" name="test_x"><failure>timeout</failure></testcase>'
        b'<testcase classname="suite" name="test_x" /></testsuite>'
    )

    cases = _parse_junit_history(report, "run")["cases"]

    assert cases[0]["successful_rerun"] is False
    assert cases[2]["successful_rerun"] is True


def test_flaky_fingerprint_is_stable_when_historical_counts_change() -> None:
    agent = FlakyTestAgent()
    first = _proposal(agent, _fixture("flaky_test.json"))
    second_context = _fixture("flaky_test.json")
    second_context.test_history.append(
        {
            "run_id": "run-11",
            "cases": [
                {"test_id": "tests.test_payment_service::test_payment_timeout", "outcome": "failed"}
            ],
        }
    )
    second = _proposal(agent, second_context)

    assert first is not None and second is not None
    assert first.fingerprint() == second.fingerprint()


def test_build_performance_uses_samples_and_reports_observed_job_only(monkeypatch) -> None:
    agent = BuildPerformanceAgent()
    context = _fixture("build_performance.json")
    observation = agent.observe(context)
    finding = observation["regressions"][0]
    proposal = _proposal(agent, context)

    assert finding["baseline_samples"] == 3
    assert finding["recent_samples"] == 3
    assert finding["baseline_seconds"] == 100
    assert finding["recent_seconds"] == 195
    assert finding["job_regressions"][0]["job"] == "build-test"
    assert proposal is not None
    assert any("attribution is unavailable" in line for line in proposal.evidence)
    monkeypatch.setenv("AGENT_BUILD_REGRESSION_THRESHOLD", "0.99")
    assert agent.observe(context)["regressions"] == []
    monkeypatch.setenv("AGENT_BUILD_REGRESSION_THRESHOLD", "invalid")
    assert agent.observe(context)["regressions"]


def test_build_performance_ignores_missing_and_malformed_metrics() -> None:
    context = AgentContext(build_history=[{"workflow": "CI", "duration_seconds": "unknown"}])
    assert BuildPerformanceAgent().observe(context)["regressions"] == []
    assert _proposal(BuildPerformanceAgent(), AgentContext()) is None


def test_test_impact_maps_direct_and_transitive_imports() -> None:
    context = AgentContext(
        changed_files=["app/services/payment_service.py"],
        source_files={
            "app/services/payment_service.py": "class PaymentService: pass\n",
            "app/main.py": "from app.services.payment_service import PaymentService\n",
        },
        test_sources={
            "tests/test_payment_integration.py": "from app.main import app\n",
            "tests/test_payment_service.py": (
                "from app.services.payment_service import PaymentService\n"
            ),
        },
    )
    observation = TestImpactAgent().observe(context)

    assert observation["unmapped_source_files"] == []
    assert "tests/test_payment_service.py" in observation["relationships"][0]["direct_tests"]
    assert "tests/test_payment_integration.py" in observation["relationships"][0]["direct_tests"]
    assert (
        "tests/test_payment_integration.py" in observation["potentially_affected_integration_tests"]
    )
    assert observation["mandatory_checks_skipped"] is False


def test_test_impact_emits_advisory_issue_for_missing_mapping() -> None:
    context = _fixture("test_impact.json")
    proposal = _proposal(TestImpactAgent(), context)

    assert proposal is not None
    assert proposal.source_agent == "test_impact_agent"
    assert proposal.assign_to_copilot is False
    assert proposal.finding_type == "unmapped_changed_modules"


def test_documentation_drift_detects_new_configuration_and_suppresses_documented() -> None:
    agent = DocumentationDriftAgent()
    context = _fixture("documentation_drift.json")
    proposal = _proposal(agent, context)

    assert proposal is not None
    assert "DEMO_PAYMENT_TIMEOUT_SECONDS_2026" in proposal.title
    documented = context.model_copy(
        update={
            "source_files": {
                **context.source_files,
                "README.md": "DEMO_PAYMENT_TIMEOUT_SECONDS_2026",
            }
        }
    )
    assert agent.observe(documented)["drift"] == []


def test_agent_effectiveness_calculates_funnel_without_assuming_merge_is_success() -> None:
    context = _fixture("agent_effectiveness.json")
    metrics = AgentEffectivenessAgent.calculate_metrics(context.telemetry)["test_quality_agent"]

    assert metrics["issue_to_pr_conversion_rate"] == 0.5
    assert metrics["pr_acceptance_rate"] == 1.0
    assert metrics["duplicate_issue_rate"] == 0.6
    assert metrics["verified_remediation_rate"] == 0.0
    assert metrics["measured_improvement_rate"] == 0.5
    assert metrics["average_issue_to_pr_hours"] == 24.0
    assert metrics["average_pr_to_merge_hours"] == 24.0
    assert metrics["incomplete_issues"] == 1
    assert metrics["rejected_issues"] == 0
    assert "flaky_test_agent" not in AgentEffectivenessAgent.calculate_metrics(context.telemetry)


def test_agent_effectiveness_handles_missing_and_malformed_telemetry() -> None:
    assert AgentEffectivenessAgent.calculate_metrics([]) == {}
    assert AgentEffectivenessAgent.calculate_metrics([{}, {"agent": 7}, {"agent": "x"}]) == {}
    malformed_ids = AgentEffectivenessAgent.calculate_metrics(
        [
            {
                "agent": "test_quality_agent",
                "event": "issue_linked_pr",
                "issue_number": [],
                "associated_pull_request": {"bad": "id"},
            }
        ]
    )["test_quality_agent"]
    assert malformed_ids["issues_with_linked_pr"] == 0
    assert malformed_ids["linked_pull_requests"] == 0


def test_effectiveness_ignores_unknown_agent_metrics() -> None:
    assert AgentEffectivenessAgent.calculate_metrics(
        [{"agent": "forged_agent", "event": "issue_created"}]
    ) == {}


def test_effectiveness_requires_minimum_evidence_for_recommendation() -> None:
    context = AgentContext(telemetry=[{"agent": "test_quality_agent", "event": "issue_created"}])
    observation = AgentEffectivenessAgent().observe(context)
    decision = AgentEffectivenessAgent().reason(context, observation)

    assert observation["findings"] == []
    assert decision.action == "no_action"


def test_shared_orchestrator_never_assigns_when_capability_denies_it(monkeypatch) -> None:
    client = GitHubClient(repository="owner/repo")
    client.dry_run = False
    monkeypatch.setattr("agents.orchestrator.orchestrator.record_telemetry", lambda _record: None)
    monkeypatch.setattr(client, "find_existing_issue", lambda _fingerprint: None)
    monkeypatch.setattr(
        client,
        "create_issue_record",
        lambda *_args: {"created": True, "number": 42, "url": "https://example.invalid/42"},
    )
    monkeypatch.setattr(
        client,
        "assign_issue_to_copilot_result",
        lambda *_args: (_ for _ in ()).throw(AssertionError("assignment must be blocked")),
    )
    proposal = _proposal(DocumentationDriftAgent(), _fixture("documentation_drift.json"))
    assert proposal is not None
    orchestrator = AgentOrchestrator(
        github_client=client,
        permissions={"documentation_drift_agent": {"create_issue": True, "assign_copilot": False}},
        approval_rules={
            "issue_policy": {
                "minimum_confidence": 0.75,
                "agents": {
                    "documentation_drift_agent": {"create_issue": True, "assign_copilot": True}
                },
            }
        },
    )

    result = orchestrator.handle_issue_proposal(proposal)

    assert result["created"] is True
    assert "assigned" not in result
    assert proposal.assign_to_copilot is False


def test_shared_orchestrator_reuses_fingerprint_without_creating_issue(monkeypatch) -> None:
    client = GitHubClient(repository="owner/repo")
    client.dry_run = False
    monkeypatch.setattr("agents.orchestrator.orchestrator.record_telemetry", lambda _record: None)
    monkeypatch.setattr(client, "find_existing_issue", lambda _fingerprint: {"number": 55})
    monkeypatch.setattr(client, "comment_on_issue", lambda *_args: True)
    monkeypatch.setattr(
        client,
        "create_issue_record",
        lambda *_args: (_ for _ in ()).throw(AssertionError("duplicate must be reused")),
    )
    proposal = _proposal(FlakyTestAgent(), _fixture("flaky_test.json"))
    assert proposal is not None
    orchestrator = AgentOrchestrator(
        github_client=client,
        permissions={"flaky_test_agent": {"create_issue": True, "assign_copilot": True}},
        approval_rules={
            "issue_policy": {
                "minimum_confidence": 0.75,
                "agents": {"flaky_test_agent": {"create_issue": True, "assign_copilot": False}},
            }
        },
    )

    result = orchestrator.handle_issue_proposal(proposal)

    assert result["reused"] is True
    assert result["number"] == 55
    assert "assigned" not in result


def test_shared_orchestrator_requires_issue_capability(monkeypatch) -> None:
    client = GitHubClient(repository="owner/repo")
    client.dry_run = False
    monkeypatch.setattr("agents.orchestrator.orchestrator.record_telemetry", lambda _record: None)
    monkeypatch.setattr(
        client,
        "create_issue_record",
        lambda *_args: (_ for _ in ()).throw(AssertionError("creation must be denied")),
    )
    proposal = _proposal(FlakyTestAgent(), _fixture("flaky_test.json"))
    assert proposal is not None
    orchestrator = AgentOrchestrator(
        github_client=client,
        permissions={"flaky_test_agent": {"create_issue": False, "assign_copilot": True}},
        approval_rules={
            "issue_policy": {
                "minimum_confidence": 0.75,
                "agents": {"flaky_test_agent": {"create_issue": True, "assign_copilot": True}},
            }
        },
    )

    result = orchestrator.handle_issue_proposal(proposal)

    assert result["created"] is False
    assert result["reason"] == "policy or confidence threshold"


def test_demo_policy_path_never_calls_github_write_endpoints(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_DRY_RUN", "true")
    monkeypatch.setattr("agents.orchestrator.orchestrator.record_telemetry", lambda _record: None)
    monkeypatch.setattr(
        "tools.github.client.requests.post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("dry-run must not write")),
    )
    client = GitHubClient(token="token", repository="owner/repo")
    proposal = _proposal(FlakyTestAgent(), _fixture("flaky_test.json"))
    assert proposal is not None
    orchestrator = AgentOrchestrator(
        github_client=client,
        permissions={"flaky_test_agent": {"create_issue": True, "assign_copilot": True}},
        approval_rules={
            "issue_policy": {
                "minimum_confidence": 0.75,
                "agents": {"flaky_test_agent": {"create_issue": True, "assign_copilot": True}},
            }
        },
    )

    result = orchestrator.handle_issue_proposal(proposal)

    assert result["dry_run"] is True
    assert result["assigned"] is True
    assert result["dry_run"] is True


def test_collector_reads_junit_and_agent_telemetry_as_data_only() -> None:
    def archive(name: str, content: bytes) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as bundle:
            bundle.writestr(name, content)
        return output.getvalue()

    junit = (
        b'<testsuites><testsuite><testcase classname="tests.test_x" name="test_a" time="bad">'
        b'<failure message="timeout"/></testcase></testsuite></testsuites>'
    )
    telemetry = b'{"agent":"flaky_test_agent","event":"issue_created"}\n'

    class ArtifactClient:
        def list_workflow_runs(
            self, per_page: int, workflow: str | None = None
        ) -> list[dict[str, Any]]:
            return [
                {
                    "id": 1,
                    "path": ".github/workflows/ci.yml@refs/heads/main",
                    "status": "completed",
                    "name": "CI",
                    "conclusion": "success",
                    "created_at": "2026-09-01T10:00:00Z",
                    "run_started_at": "2026-09-01T10:00:00Z",
                    "updated_at": "2026-09-01T10:01:00Z",
                },
                {
                    "id": 2,
                    "path": ".github/workflows/agent-flaky-test.yml@refs/heads/main",
                    "status": "completed",
                    "name": "Agent - Flaky Test Analysis",
                },
            ]

        def list_run_artifacts(self, run_id: int) -> list[dict[str, Any]]:
            artifact_id = 10 if run_id == 1 else 20
            return [
                {"id": artifact_id, "name": "test-results" if run_id == 1 else "agent-telemetry"}
            ]

        def download_run_artifact(self, artifact_id: int) -> bytes:
            return (
                archive("junit.xml", junit)
                if artifact_id == 10
                else archive("agent_telemetry.ndjson", telemetry)
            )

        def get_workflow_jobs(self, run_id: int) -> list[dict[str, Any]]:
            return [
                {
                    "name": "build-test",
                    "started_at": "2026-09-01T10:00:00Z",
                    "completed_at": "2026-09-01T10:01:00Z",
                    "conclusion": "success",
                }
            ]

        def list_agent_issue_lifecycle(self, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return []

    tests, builds, records, _runs = _collect_github_history(ArtifactClient())  # type: ignore[arg-type]

    assert tests[0]["cases"][0]["outcome"] == "failed"
    assert tests[0]["cases"][0]["duration_seconds"] == 0.0
    assert builds[0]["duration_seconds"] == 60
    assert "stage_durations" not in builds[0]
    assert records[0]["agent"] == "flaky_test_agent"


def test_github_history_read_failures_degrade_to_empty(monkeypatch) -> None:
    class Forbidden:
        status_code = 403
        text = "forbidden"

    client = GitHubClient(token="token", repository="owner/repo")
    monkeypatch.setattr("tools.github.client.requests.get", lambda *_args, **_kwargs: Forbidden())

    assert client.list_workflow_runs() == []
    assert client.list_agent_issue_lifecycle() == []


def test_github_issue_reconciliation_records_pr_and_human_outcomes(monkeypatch) -> None:
    class Response:
        def __init__(self, payload: Any) -> None:
            self.payload = payload

        def json(self) -> Any:
            return self.payload

    responses = {
        "issues": [
            {
                "number": 71,
                "body": (
                    "<!-- evolutionary-cicd:source-agent=flaky_test_agent -->\n"
                    "<!-- evolutionary-cicd:fingerprint=abc123 -->"
                ),
                "created_at": "2026-09-01T10:00:00Z",
                "updated_at": "2026-09-03T10:00:00Z",
                "state": "closed",
                "labels": [
                    {"name": "agent-remediation-verified"},
                    {"name": "agent-measured-target-met"},
                ],
            }
        ],
        "issues/71/timeline": [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {"number": 72, "pull_request": {"url": "https://api.github.com"}}
                },
            }
        ],
        "pulls/72": {
            "number": 72,
            "created_at": "2026-09-02T10:00:00Z",
            "merged_at": "2026-09-03T10:00:00Z",
            "merged": True,
        },
    }
    client = GitHubClient(token="token", repository="owner/repo")
    monkeypatch.setattr(
        client,
        "_api_get",
        lambda path, _params=None: Response(responses[path]) if path in responses else None,
    )

    events = client.list_agent_issue_lifecycle(
        [
            {
                "status": "completed",
                "conclusion": "success",
                "updated_at": "2026-09-04T00:00:00Z",
                "pull_requests": [{"number": 72}],
            }
        ]
    )

    assert {event["event"] for event in events} == {
        "issue_created",
        "issue_linked_pr",
        "pr_merged",
        "pr_validation_result",
        "remediation_verified",
        "measured_outcome",
    }
    assert (
        next(event for event in events if event["event"] == "pr_validation_result")[
            "pr_validation_result"
        ]
        == "success"
    )
