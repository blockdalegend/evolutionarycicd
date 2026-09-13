from __future__ import annotations

from agents.base import AgentContext, AgentDecision
from agents.supply_chain.agent import SupplyChainAgent
from llm.models import LLMResponse


def test_observe_propagates_github_actions_scan_findings() -> None:
    context = AgentContext(
        security_findings={
            "github_actions": [
                {
                    "ident": "dangerous-triggers",
                    "determinations": {"severity": "High"},
                }
            ],
            "github_actions_scan_status": "completed",
        }
    )

    observation = SupplyChainAgent().observe(context)

    assert observation["github_actions_findings"][0]["ident"] == "dangerous-triggers"
    assert observation["github_actions_scan_status"] == "completed"


def test_reason_preserves_deterministic_finding_details(monkeypatch) -> None:
    monkeypatch.setattr(
        "agents.base.LLMClient.complete",
        lambda _client, _request: LLMResponse(
            success=True,
            parsed={
                "action": "report_supply_chain_findings",
                "reason": "The audit found multiple issues.",
                "tool": "comment_pull_request",
                "arguments": {},
                "confidence": 0.8,
                "requires_approval": False,
            },
        ),
    )
    context = AgentContext(
        security_findings={
            "github_actions": [
                {
                    "ident": "dangerous-triggers",
                    "determinations": {"severity": "High"},
                    "location": ".github/workflows/ci.yml:12",
                }
            ],
            "github_actions_scan_status": "completed",
        }
    )

    decision = SupplyChainAgent().reason(context, SupplyChainAgent().observe(context))

    assert "Zizmor: dangerous-triggers" in decision.reason
    assert "severity: High" in decision.reason
    assert "location: .github/workflows/ci.yml:12" in decision.reason
    assert "Zizmor scan status: completed" in decision.reason


def test_render_scanner_details_preserves_vulnerability_evidence() -> None:
    observation = {
        "pip_audit_findings": [
            {
                "name": "python-dotenv",
                "version": "1.2.1",
                "vulns": [
                    {
                        "id": "GHSA-example",
                        "aliases": ["CVE-2026-1234"],
                        "fix_versions": ["1.2.2"],
                        "description": "Example vulnerability description.",
                    }
                ],
            }
        ],
        "bandit_findings": [
            {
                "test_id": "B602",
                "issue_text": " subprocess call with shell=True identified",
                "issue_severity": "High",
                "issue_confidence": "High",
                "filename": "scripts/collect_pipeline_context.py",
                "line_number": 22,
            }
        ],
        "github_actions_findings": [],
        "github_actions_scan_status": "completed",
        "unpinned_requirements": [],
        "unpinned_actions": [],
    }

    rendered = SupplyChainAgent._render_scanner_details(observation)

    assert "python-dotenv" in rendered
    assert "installed version: 1.2.1" in rendered
    assert "GHSA-example" in rendered
    assert "CVE-2026-1234" in rendered
    assert "fix versions: ['1.2.2']" in rendered
    assert "B602" in rendered
    assert "severity: High" in rendered
    assert "scripts/collect_pipeline_context.py:22" in rendered


def test_reason_accepts_evidence_backed_pinned_fix(monkeypatch) -> None:
    requirements = "requests==2.33.0\n# exact version pin\n"
    monkeypatch.setattr(
        "agents.base.LLMClient.complete",
        lambda _client, _request: LLMResponse(
            success=True,
            parsed={
                "action": "propose_supply_chain_fix_pull_request",
                "reason": "Pin the dependency to the reviewed version.",
                "tool": "create_pull_request",
                "arguments": {
                    "proposed_changes": [
                        {
                            "path": "requirements.txt",
                            "content": requirements,
                            "rationale": "Pin the dependency to prevent mutable resolution.",
                            "finding_locations": ["requests"],
                        }
                    ]
                },
                "confidence": 0.9,
                "requires_approval": True,
            },
        ),
    )
    agent = SupplyChainAgent()
    observation = {
        "unpinned_requirements": ["requests"],
        "unpinned_actions": [],
        "pip_audit_findings": [],
        "bandit_findings": [],
        "github_actions_findings": [],
        "github_actions_scan_status": "completed",
        "repair_files": {"requirements.txt": requirements},
    }

    decision = agent.reason(AgentContext(), observation)

    assert decision.tool == "create_pull_request"
    assert decision.requires_approval is True
    assert decision.arguments["proposed_changes"][0]["path"] == "requirements.txt"


def test_invalid_supply_chain_fix_is_rejected() -> None:
    observation = {
        "repair_files": {"requirements.txt": "requests==2.33.0\n"},
    }

    try:
        SupplyChainAgent._validate_proposed_changes(
            [
                {
                    "path": "requirements.txt",
                    "content": "requests\n# still unpinned\n",
                    "rationale": "This is a sufficiently detailed rationale.",
                    "finding_locations": ["requests"],
                }
            ],
            observation,
        )
    except ValueError as exc:
        assert "unpinned" in str(exc)
    else:
        raise AssertionError("unpinned candidate should be rejected")


def test_fix_pr_requires_explicit_enablement(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_ALLOW_FIX_PR", raising=False)
    decision = AgentDecision(
        action="propose_supply_chain_fix_pull_request",
        reason="A reviewed fix is available.",
        tool="create_pull_request",
        arguments={
            "proposed_changes": [
                {
                    "path": "requirements.txt",
                    "content": "requests==2.33.0\n",
                }
            ]
        },
        requires_approval=True,
    )

    result = SupplyChainAgent().act(AgentContext(repository="owner/repo"), decision)

    assert result["created"] is False
    assert result["dry_run"] is True
    assert result["approval_required"] is True
