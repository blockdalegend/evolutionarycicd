from __future__ import annotations

from agents.base import AgentContext
from agents.pipeline_optimizer.agent import PipelineOptimizerAgent
from llm.client import LLMClient
from llm.models import LLMResponse


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


def test_pipeline_optimizer_rejects_generic_llm_reason(monkeypatch) -> None:
    monkeypatch.setattr(
        LLMClient,
        "complete",
        lambda *_args: LLMResponse(
            success=True,
            parsed={
                "action": "workflow_failure",
                "reason": "workflow_failure",
                "arguments": {},
                "confidence": 0.9,
                "requires_approval": False,
            },
        ),
    )
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
        ]
    )

    decision = PipelineOptimizerAgent().reason(context, {"runs": context.pipeline_history})

    assert decision.reason.startswith("Analyzed 2 historical runs")
    assert decision.arguments["recommended_fixes"]


def test_pipeline_optimizer_uses_substantive_llm_analysis(monkeypatch) -> None:
    monkeypatch.setattr(
        LLMClient,
        "complete",
        lambda *_args: LLMResponse(
            success=True,
            parsed={
                "summary": (
                    "The pytest stage is failing for a recurring payment gateway timeout, "
                    "while successful runs show the workflow can complete."
                ),
                "findings": [
                    {
                        "category": "flaky_test",
                        "evidence": (
                            "ci.yml pytest flaky:test_payment_gateway_timeout occurred twice"
                        ),
                        "analysis": (
                            "ci.yml's pytest failures are likely timing-sensitive because "
                            "the same gateway timeout recurs across separate runs."
                        ),
                        "recommended_fix": (
                            "Replace the live gateway timing dependency with a deterministic "
                            "fixture in the payment gateway test, then quarantine only the "
                            "identified test until that change is reviewed."
                        ),
                    }
                ],
            },
        ),
    )
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
        ]
    )

    decision = PipelineOptimizerAgent().reason(context, {"runs": context.pipeline_history})

    assert decision.arguments["analysis_source"] == "llm"
    assert "timing-sensitive" in decision.reason
    assert "deterministic fixture" in decision.reason