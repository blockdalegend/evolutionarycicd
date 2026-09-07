from __future__ import annotations

from agents.base import AgentContext
from agents.failure_analysis.agent import FailureAnalysisAgent
from agents.test_quality.agent import TestQualityAgent
from llm.client import LLMClient
from llm.models import LLMResponse
from tools.security.scanners import run_zizmor


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


def test_test_quality_report_includes_authoritative_pytest_results(monkeypatch) -> None:
    monkeypatch.setattr(
        "agents.base.LLMClient.complete",
        lambda _client, _request: LLMResponse(
            success=False,
            error="unavailable",
        ),
    )
    context = AgentContext(test_results={"tests": 27, "failures_count": 0})

    decision = TestQualityAgent().reason(context, TestQualityAgent().observe(context))

    assert "27 tests executed, 0 failure(s)" in decision.reason


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


def test_test_quality_requests_report_when_decision_omits_it(monkeypatch) -> None:
    responses = iter(
        [
            LLMResponse(
                success=True,
                parsed={
                    "action": "no_action",
                    "reason": "Tests are adequate.",
                    "arguments": {},
                    "confidence": 0.8,
                    "requires_approval": False,
                },
            ),
            LLMResponse(
                success=True,
                parsed={
                    "rating": "Good",
                    "score": 82,
                    "assertions": "Assertions verify returned values and policy outcomes.",
                    "behavior_coverage": "Core paths are covered; edge cases remain.",
                    "isolation_mocking": "External GitHub and LLM calls are isolated.",
                    "reliability": "Tests are deterministic.",
                    "weak_tests": [],
                    "missing_behaviors": ["Malformed context input"],
                    "recommendations": ["Add malformed context coverage"],
                },
            ),
        ]
    )
    monkeypatch.setattr("agents.base.LLMClient.complete", lambda *_args: next(responses))
    context = AgentContext(test_results={"tests": 27, "failures_count": 0})

    decision = TestQualityAgent().reason(context, TestQualityAgent().observe(context))

    assert decision.arguments["quality_report"]["score"] == 82
    assert "Malformed context input" in decision.reason


def test_test_quality_uses_transparent_fallback_when_report_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        "agents.base.LLMClient.complete",
        lambda _client, _request: LLMResponse(success=False, error="unavailable"),
    )
    context = AgentContext(
        test_results={"tests": 30, "failures_count": 0},
        test_sources={"tests/test_weak.py": "def test_weak(): assert True"},
    )

    decision = TestQualityAgent().reason(context, TestQualityAgent().observe(context))
    report = decision.arguments["quality_report"]

    assert report["rating"] == "Needs improvement"
    assert "tests/test_weak.py" in report["weak_tests"][0]
    assert "LLM assessment unavailable" in report["behavior_coverage"]


def test_test_quality_accepts_json_wrapped_in_markdown(monkeypatch) -> None:
    report_json = """```json
    {
      "rating": "Good",
      "score": 80,
      "assertions": "Behavior is asserted.",
      "behavior_coverage": "Core paths are covered.",
      "isolation_mocking": "Dependencies are isolated.",
      "reliability": "Tests are deterministic.",
      "weak_tests": [],
      "missing_behaviors": [],
      "recommendations": []
    }
    ```"""
    monkeypatch.setattr(
        "agents.base.LLMClient.complete",
        lambda _client, _request: LLMResponse(
            success=False,
            content=report_json,
            error="failed to parse structured output",
        ),
    )
    context = AgentContext(test_results={"tests": 30, "failures_count": 0})

    decision = TestQualityAgent().reason(context, TestQualityAgent().observe(context))

    assert decision.arguments["quality_report"]["score"] == 80
    assert "Behavior is asserted." in decision.reason


def test_test_quality_normalizes_provider_report_field_names(monkeypatch) -> None:
    monkeypatch.setattr(
        "agents.base.LLMClient.complete",
        lambda _client, _request: LLMResponse(
            success=True,
            parsed={
                "overall_rating": "Good",
                "quality_score": 80,
                "assertion_analysis": "Behavior is asserted.",
                "coverage_analysis": "Core paths are covered.",
                "mocking_analysis": "Dependencies are isolated.",
                "reliability_analysis": "Tests are deterministic.",
            },
        ),
    )
    context = AgentContext(test_results={"tests": 30, "failures_count": 0})

    decision = TestQualityAgent().reason(context, TestQualityAgent().observe(context))

    assert decision.arguments["quality_report"]["rating"] == "Good"
    assert decision.arguments["quality_report"]["score"] == 80


def test_llm_client_uses_defaults_for_empty_environment(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")

    client = LLMClient()

    assert client.base_url == "https://api.openai.com/v1"
    assert client.model == "gpt-4o-mini"


def test_llm_client_uses_azure_api_key_auth(monkeypatch) -> None:
    client = LLMClient(api_key="test-key", auth_mode="api-key")

    assert client._auth_headers() == {"api-key": "test-key"}


def test_zizmor_parses_action_security_findings(monkeypatch, tmp_path) -> None:
    class _Completed:
        stdout = '[{"ident": "dangerous-triggers", "determinations": {"severity": "High"}}]'

    monkeypatch.setattr("tools.security.scanners.which", lambda _name: "/usr/local/bin/zizmor")
    monkeypatch.setattr(
        "tools.security.scanners.subprocess.run", lambda *args, **kwargs: _Completed()
    )

    result = run_zizmor(tmp_path)

    assert result.ran_successfully is True
    assert result.findings[0]["ident"] == "dangerous-triggers"


def test_zizmor_reports_when_unavailable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("tools.security.scanners.which", lambda _name: None)

    result = run_zizmor(tmp_path)

    assert result.ran_successfully is False
    assert result.findings == []