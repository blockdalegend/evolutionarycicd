from __future__ import annotations

from agents.base import AgentContext, AgentDecision
from agents.issue_proposals import IssueProposal, proposal_from_decision
from agents.orchestrator.orchestrator import AgentOrchestrator
from tools.github.client import GitHubClient


def test_issue_proposal_is_stable_and_rendered_as_a_work_order() -> None:
    proposal = IssueProposal(
        title="[Test Quality] Add validation tests",
        summary="Validation paths lack coverage.",
        problem="New validation branches are untested.",
        evidence=["coverage: 76.8%"],
        affected_files=["app/services/payment_service.py"],
        requested_change="Add focused tests without changing production behavior.",
        acceptance_criteria=["All new paths are covered"],
        validation_commands=["pytest"],
        labels=["agent-recommendation"],
        source_agent="test_quality_agent",
        confidence=0.94,
        assign_to_copilot=True,
    )
    assert proposal.fingerprint() == proposal.model_copy().fingerprint()
    body = proposal.render_markdown()
    assert "evolutionary-cicd:fingerprint=" in body
    assert "Copilot may implement" in body
    assert "pytest" in body


def test_issue_proposal_rejects_invalid_confidence() -> None:
    try:
        IssueProposal(
            title="x",
            summary="x",
            problem="x",
            requested_change="x",
            source_agent="x",
            confidence=1.1,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("invalid confidence should be rejected")


def test_agent_run_attaches_issue_proposal_without_executing_remediation() -> None:
    from agents.failure_analysis.agent import FailureAnalysisAgent

    result = FailureAnalysisAgent().run(
        AgentContext(
            changed_files=["app/services/payment_service.py"],
            test_results={"failures": ["app/services/payment_service.py: assertion failed"]},
        )
    )
    assert result.success
    assert result.artifacts["issue_proposal"]["source_agent"] == "failure_analysis_agent"


def test_pipeline_optimizer_proposal_requests_copilot_assignment() -> None:
    proposal = proposal_from_decision(
        "pipeline_optimizer_agent",
        AgentContext(),
        {"runs": [{"workflow": "ci", "status": "failure"}]},
        AgentDecision(
            action="recommend_pipeline_improvements",
            reason="The CI workflow has a recurring failure pattern.",
            confidence=0.8,
            requires_approval=True,
        ),
    )

    assert proposal is not None
    assert proposal.assign_to_copilot is True


def test_merge_conflict_proposal_requests_copilot_assignment() -> None:
    proposal = proposal_from_decision(
        "merge_conflict_agent",
        AgentContext(),
        {"conflicts": ["app/main.py"]},
        AgentDecision(
            action="resolve_merge_conflict",
            reason="The pull request contains an unresolved merge conflict.",
            confidence=0.85,
            requires_approval=True,
        ),
    )

    assert proposal is not None
    assert proposal.assign_to_copilot is True


def test_proposal_evidence_excludes_large_repair_payloads() -> None:
    proposal = proposal_from_decision(
        "supply_chain_agent",
        AgentContext(),
        {
            "pip_audit_findings": [{"name": "example-package"}],
            "repair_files": {".github/workflows/ci.yml": "x" * 100000},
        },
        AgentDecision(
            action="report_supply_chain_findings",
            reason="A scanner reported a dependency issue.",
            confidence=0.8,
        ),
    )

    assert proposal is not None
    assert all("repair_files" not in item for item in proposal.evidence)
    assert len(proposal.render_markdown()) < 20000


def _proposal(confidence: float = 0.9) -> IssueProposal:
    return IssueProposal(
        title="Actionable finding",
        summary="A concrete finding",
        problem="A concrete problem",
        requested_change="Make the bounded change",
        source_agent="test_quality_agent",
        confidence=confidence,
    )


def test_policy_threshold_withholds_low_confidence_issue() -> None:
    orchestrator = AgentOrchestrator(
        github_client=GitHubClient(),
        permissions={"test_quality_agent": {"create_issue": True}},
        approval_rules={"issue_policy": {"minimum_confidence": 0.8}},
    )
    assert orchestrator.handle_issue_proposal(_proposal(0.79))["created"] is False


def test_dry_run_issue_and_copilot_assignment_are_write_free() -> None:
    client = GitHubClient(token="", repository="")
    orchestrator = AgentOrchestrator(
        github_client=client,
        permissions={"test_quality_agent": {"create_issue": True, "assign_copilot": True}},
        approval_rules={
            "issue_policy": {
                "minimum_confidence": 0.8,
                "agents": {"test_quality_agent": {"create_issue": True, "assign_copilot": True}},
            }
        },
    )
    result = orchestrator.handle_issue_proposal(_proposal())
    assert result["dry_run"] is True
