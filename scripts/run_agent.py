"""CLI entry point to run a single agent end-to-end (used by workflows/demo).

Usage:
    python scripts/run_agent.py --agent test_quality
    python scripts/run_agent.py --agent pipeline_optimizer
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.base import AgentContext, BaseAgent  # noqa: E402
from agents.failure_analysis.agent import FailureAnalysisAgent  # noqa: E402
from agents.merge_conflict.agent import MergeConflictAgent  # noqa: E402
from agents.orchestrator.orchestrator import AgentOrchestrator  # noqa: E402
from agents.pipeline_optimizer.agent import PipelineOptimizerAgent  # noqa: E402
from agents.supply_chain.agent import SupplyChainAgent  # noqa: E402
from agents.test_quality.agent import TestQualityAgent  # noqa: E402
from scripts.collect_pipeline_context import collect_context  # noqa: E402
from tools.github.client import GitHubClient  # noqa: E402

AGENTS: dict[str, type[BaseAgent]] = {
    "test_quality": TestQualityAgent,
    "failure_analysis": FailureAnalysisAgent,
    "merge_conflict": MergeConflictAgent,
    "supply_chain": SupplyChainAgent,
    "pipeline_optimizer": PipelineOptimizerAgent,
}


def main() -> int:
    """Run the requested agent against locally collected context."""
    parser = argparse.ArgumentParser(description="Run a single evolutionary-cicd agent.")
    parser.add_argument("--agent", choices=sorted(AGENTS), required=True)
    args = parser.parse_args()

    agent_cls = AGENTS[args.agent]
    agent = agent_cls()
    context = AgentContext.model_validate(collect_context())

    orchestrator = AgentOrchestrator()
    result = orchestrator.run_agent(agent, context)

    print(f"success={result.success}")
    print(result.message)

    if args.agent == "pipeline_optimizer" and result.success:
        decision = result.artifacts.get("decision", {})
        recommendations = decision.get("arguments", {}).get("recommendations", [])
        if recommendations:
            github_client = GitHubClient()
            body = (
                "## Evolutionary CI/CD: Pipeline Optimization Recommendations\n\n"
                + "\n".join(f"- {rec}" for rec in recommendations)
                + "\n\nThese recommendations require human review before any workflow "
                "file is changed.\n"
            )
            github_client.create_issue(
                title="Evolutionary CI/CD: pipeline optimization recommendations",
                body=body,
                labels=["agent-recommendation"],
            )

    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
