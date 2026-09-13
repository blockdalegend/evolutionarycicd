from __future__ import annotations

from agents.base import AgentContext
from agents.pipeline_optimizer.agent import PipelineOptimizerAgent


def test_pipeline_optimizer_provides_recommended_fixes() -> None:
    context = AgentContext(
        pipeline_history=[
            {
                "workflow": "ci.yml",
                "status": "failure",
                "duration_seconds": 20,
                "failed_stage": "pytest",
                "failure_reason": "flaky:test_payment_gateway_timeout",
                "retry_count": 1,
            },
            {
                "workflow": "ci.yml",
                "status": "failure",
                "duration_seconds": 22,
                "failed_stage": "pytest",
                "failure_reason": "flaky:test_payment_gateway_timeout",
                "retry_count": 1,
            },
            {
                "workflow": "ci.yml",
                "status": "success",
                "duration_seconds": 18,
                "retry_count": 0,
            },
        ]
    )

    decision = PipelineOptimizerAgent().reason(context, {"runs": context.pipeline_history})

    fixes = decision.arguments["recommended_fixes"]
    assert fixes
    assert all(item["recommendation"] for item in fixes)
    assert all(item["recommended_fix"] for item in fixes)
    assert any("deterministic fixture" in item["recommended_fix"] for item in fixes)