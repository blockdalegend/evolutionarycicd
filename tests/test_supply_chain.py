from __future__ import annotations

from agents.base import AgentContext
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
