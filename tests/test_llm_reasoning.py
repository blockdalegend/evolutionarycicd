from __future__ import annotations

from agents.base import AgentContext
from agents.failure_analysis.agent import FailureAnalysisAgent
from agents.test_quality.agent import TestQualityAgent
from llm.client import LLMClient
from llm.models import LLMResponse


def test_agent_uses_structured_llm_decision(monkeypatch) -> None:
    def complete(_client, _request):
        return LLMResponse(
            success=True,
            parsed={
                "action": "report_failure_analysis",
                "reason": "The changed payment service is implicated by the failure.",
                "tool": "comment_pull_request",
                "arguments": {"suspects": ["app/services/payment_service.py"]},
                "confidence": 0.91,
                "requires_approval": False,
            },
        )

    monkeypatch.setattr("agents.base.LLMClient.complete", complete)
    context = AgentContext(
        changed_files=["app/services/payment_service.py"],
        test_results={"failures": ["app/services/payment_service.py::test_charge failed"]},
    )

    result = FailureAnalysisAgent().reason(context, FailureAnalysisAgent().observe(context))

    assert result.reason.startswith("The changed payment service")
    assert result.tool == "comment_pull_request"
    assert result.confidence == 0.91


def test_agent_falls_back_when_llm_is_unavailable(monkeypatch) -> None:
    def complete(_client, _request):
        return LLMResponse(success=False, error="unavailable")

    monkeypatch.setattr("agents.base.LLMClient.complete", complete)
    context = AgentContext(diff="amount == 0")
    agent = TestQualityAgent()

    decision = agent.reason(context, agent.observe(context))

    assert decision.action == "propose_tests"
    assert decision.tool == "execute_tests"


def test_test_quality_observation_includes_test_source() -> None:
    context = AgentContext(
        test_sources={"tests/test_payment.py": "def test_charge(): assert True"}
    )

    observation = TestQualityAgent().observe(context)

    assert observation["test_sources"] == context.test_sources


def test_test_quality_llm_receives_test_source(monkeypatch) -> None:
    captured = {}

    def complete(_client, request):
        captured["content"] = request.messages[1].content
        return LLMResponse(
            success=True,
            parsed={
                "action": "report_test_quality",
                "reason": "Needs improvement: the test uses an unconditional assertion.",
                "arguments": {
                    "quality_report": {
                        "rating": "Poor",
                        "score": 15,
                        "weak_tests": ["test_weak"],
                    }
                },
                "confidence": 0.95,
                "requires_approval": False,
            },
        )

    monkeypatch.setattr("agents.base.LLMClient.complete", complete)
    context = AgentContext(test_sources={"tests/test_weak.py": "assert True"})

    decision = TestQualityAgent().reason(context, TestQualityAgent().observe(context))

    assert "tests/test_weak.py" in captured["content"]
    assert decision.arguments["quality_report"]["rating"] == "Poor"
    assert "Quality rating: Poor (15/100)" in decision.reason


def test_llm_client_uses_defaults_for_empty_environment(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")

    client = LLMClient()

    assert client.base_url == "https://api.openai.com/v1"
    assert client.model == "gpt-4o-mini"


def test_llm_client_uses_azure_api_key_auth(monkeypatch) -> None:
    client = LLMClient(api_key="test-key", auth_mode="api-key")

    assert client._auth_headers() == {"api-key": "test-key"}