from __future__ import annotations

from agents.base import AgentContext
from agents.supply_chain.agent import SupplyChainAgent


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
