from __future__ import annotations

import pytest

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
                        "assertions": "test_weak uses an unconditional assertion.",
                        "behavior_coverage": "test_weak does not verify application behavior.",
                        "isolation_mocking": "No dependency isolation is shown in test_weak.",
                        "reliability": (
                            "The unconditional assertion can pass without executing behavior."
                        ),
                        "findings": [
                            {
                                "category": "assertions",
                                "location": "tests/test_weak.py",
                                "current_behavior": (
                                    "The test executes assert True without calling "
                                    "application code."
                                ),
                                "gap": (
                                    "It has no assertion tied to the behavior the "
                                    "test claims to cover."
                                ),
                                "why_it_matters": (
                                    "The test can pass even when the application "
                                    "behavior is broken."
                                ),
                                "recommended_test": (
                                    "Replace it in tests/test_weak.py with a test "
                                    "of the returned outcome."
                                ),
                                "expected_assertion": (
                                    "Assert the expected result for the exercised input."
                                ),
                            },
                            {
                                "category": "behavior_coverage",
                                "location": "tests/test_weak.py",
                                "current_behavior": (
                                    "The file contains only an unconditional assertion."
                                ),
                                "gap": (
                                    "No test in this file exercises a visible application branch."
                                ),
                                "why_it_matters": (
                                    "Important behavior can regress without a failing test."
                                ),
                                "recommended_test": (
                                    "Add a named behavior test in tests/test_weak.py."
                                ),
                                "expected_assertion": "Assert the branch's documented result.",
                            },
                            {
                                "category": "isolation_mocking",
                                "location": "tests/test_weak.py",
                                "current_behavior": (
                                    "The file does not call or isolate an external dependency."
                                ),
                                "gap": (
                                    "No mock boundary is demonstrated for dependencies used by "
                                    "the behavior."
                                ),
                                "why_it_matters": (
                                    "Uncontrolled dependencies can make the test slow or flaky."
                                ),
                                "recommended_test": (
                                    "Mock the dependency in tests/test_weak.py if the behavior "
                                    "calls one."
                                ),
                                "expected_assertion": (
                                    "Assert the outcome using the controlled mock response."
                                ),
                            },
                        ],
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
                        "assertions": (
                            "Assertions verify returned values and policy outcomes "
                            "for the exercised paths."
                        ),
                        "behavior_coverage": (
                            "Core paths are covered; edge cases remain visible in "
                            "the supplied diff."
                        ),
                        "isolation_mocking": (
                            "External GitHub and LLM calls are isolated by the "
                            "supplied test setup."
                        ),
                        "reliability": (
                            "The supplied test results indicate a deterministic "
                            "completed run."
                        ),
                        "findings": [],
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
    assert "semantic coverage was not assessed" in report["behavior_coverage"]


def test_test_quality_accepts_json_wrapped_in_markdown(monkeypatch) -> None:
    report_json = """```json
    {
      "rating": "Good",
      "score": 80,
          "assertions": "Behavior is asserted through returned values and errors.",
          "behavior_coverage": "Core paths are covered by the supplied test results.",
          "isolation_mocking": "Dependencies are isolated in the supplied test setup.",
          "reliability": "The supplied tests complete deterministically without failures.",
          "findings": [],
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
    assert "Behavior is asserted through returned values" in decision.reason


def test_test_quality_normalizes_provider_report_field_names(monkeypatch) -> None:
    monkeypatch.setattr(
        "agents.base.LLMClient.complete",
        lambda _client, _request: LLMResponse(
            success=True,
            parsed={
                "action": "report_test_quality",
                "reason": "The supplied tests provide useful behavioral evidence.",
                "arguments": {
                    "quality_report": {
                        "overall_rating": "Good",
                        "quality_score": 80,
                        "assertion_analysis": (
                            "Behavior is asserted through returned values and errors."
                        ),
                        "coverage_analysis": (
                            "Core paths are covered by the supplied test results."
                        ),
                        "mocking_analysis": (
                            "Dependencies are isolated in the supplied test setup."
                        ),
                        "reliability_analysis": (
                            "The supplied tests complete deterministically without failures."
                        ),
                        "findings": [],
                    }
                },
                "confidence": 0.8,
                "requires_approval": False,
            },
        ),
    )
    context = AgentContext(test_results={"tests": 30, "failures_count": 0})

    decision = TestQualityAgent().reason(context, TestQualityAgent().observe(context))

    assert decision.arguments["quality_report"]["rating"] == "Good"
    assert decision.arguments["quality_report"]["score"] == 80


def test_test_quality_rejects_numeric_or_shallow_report(monkeypatch) -> None:
    responses = iter(
        [
            LLMResponse(
                success=True,
                parsed={
                    "action": "no_action",
                    "reason": "I need help generating a comprehensive list of gaps.",
                    "arguments": {
                        "quality_report": {
                            "rating": 4,
                            "score": 4,
                            "assertions": "testing",
                            "behavior_coverage": 90,
                            "isolation_mocking": 90,
                            "reliability": 90,
                        }
                    },
                    "confidence": 0.5,
                    "requires_approval": False,
                },
            ),
            LLMResponse(
                success=True,
                parsed={
                    "rating": "Needs improvement",
                    "score": 60,
                    "assertions": "Tests assert key returned values and errors.",
                    "behavior_coverage": (
                        "Core paths pass; two visible boundary branches lack tests."
                    ),
                    "isolation_mocking": "The supplied tests do not use external dependencies.",
                    "reliability": "Pytest completed successfully with deterministic local tests.",
                        "findings": [],
                    "weak_tests": [],
                    "missing_behaviors": ["zero-dollar transaction branch"],
                    "recommendations": ["Test the zero-dollar transaction branch."],
                },
            ),
        ]
    )
    monkeypatch.setattr("agents.base.LLMClient.complete", lambda *_args: next(responses))

    decision = TestQualityAgent().reason(
        AgentContext(test_results={"tests": 34, "failures_count": 0}),
        TestQualityAgent().observe(AgentContext(test_results={"tests": 34})),
    )

    report = decision.arguments["quality_report"]
    assert report["rating"] == "Needs improvement"
    assert report["score"] == 60
    assert report["assertions"] != "testing"


def test_test_quality_preserves_llm_finding_detail(monkeypatch) -> None:
    monkeypatch.setattr(
        "agents.base.LLMClient.complete",
        lambda *_args: LLMResponse(
            success=True,
            parsed={
                "action": "no_action",
                "reason": "The tests are mostly meaningful.",
                "arguments": {
                    "quality_report": {
                        "rating": "Good",
                        "score": 80,
                        "assertions": "Tests assert returned values and expected exceptions.",
                        "behavior_coverage": "Core behavior is covered, with one visible gap.",
                        "isolation_mocking": "The tests use local objects without network calls.",
                        "reliability": "The suite is deterministic and completed successfully.",
                        "findings": [
                            {
                                "category": "assertions",
                                "location": "tests/test_quality.py::test_source",
                                "current_behavior": (
                                    "test_source executes an unconditional assertion."
                                ),
                                "gap": (
                                    "The test does not verify any application behavior or result."
                                ),
                                "why_it_matters": (
                                    "It can pass while the behavior under review is broken."
                                ),
                                "recommended_test": (
                                    "Replace it in tests/test_quality.py with a payment "
                                    "outcome test."
                                ),
                                "expected_assertion": (
                                    "Assert the returned approval value is False for invalid input."
                                ),
                            },
                            {
                                "category": "behavior_coverage",
                                "location": "tests/test_quality.py::test_source",
                                "current_behavior": "test_source executes the visible test body.",
                                "gap": "The test does not cover the relevant behavior branch.",
                                "why_it_matters": (
                                    "A missing branch test can allow a regression to pass."
                                ),
                                "recommended_test": (
                                    "Add the branch case to tests/test_quality.py::test_source."
                                ),
                                "expected_assertion": "Assert the expected result for that branch.",
                            },
                            {
                                "category": "isolation_mocking",
                                "location": "tests/test_quality.py::test_source",
                                "current_behavior": (
                                    "test_source uses only local inputs and no external calls."
                                ),
                                "gap": (
                                    "The test does not demonstrate an isolation boundary for "
                                    "dependencies."
                                ),
                                "why_it_matters": (
                                    "Uncontrolled dependencies can make the test nondeterministic."
                                ),
                                "recommended_test": (
                                    "Add a mock in tests/test_quality.py if the branch calls "
                                    "an external dependency."
                                ),
                                "expected_assertion": (
                                    "Assert the result from the controlled mock response."
                                ),
                            },
                        ],
                        "weak_tests": [],
                        "missing_behaviors": [],
                        "recommendations": [],
                    }
                },
                "confidence": 0.8,
                "requires_approval": False,
            },
        ),
    )
    context = AgentContext(
        diff="amount == 0",
        test_sources={"tests/test_quality.py": "def test_source(): assert True"},
    )

    decision = TestQualityAgent().reason(context, TestQualityAgent().observe(context))
    report = decision.arguments["quality_report"]

    assert report["findings"][0]["location"] == "tests/test_quality.py::test_source"
    assert "payment outcome" in report["findings"][0]["recommended_test"]
    assert "Detailed findings:" in decision.reason


def test_test_quality_rejects_finding_outside_context(monkeypatch) -> None:
    monkeypatch.setattr(
        "agents.base.LLMClient.complete",
        lambda *_args: LLMResponse(
            success=True,
            parsed={
                "action": "report_test_quality",
                "reason": "The supplied tests need a specific behavioral check.",
                "arguments": {
                    "quality_report": {
                        "rating": "Needs improvement",
                        "score": 60,
                        "assertions": "The supplied test source contains a weak assertion.",
                        "behavior_coverage": "The supplied context shows only a narrow test path.",
                        "isolation_mocking": (
                            "No external dependency is visible in the supplied source."
                        ),
                        "reliability": "The local assertion is deterministic but not meaningful.",
                        "findings": [
                            {
                                "category": "assertions",
                                "location": "tests/missing.py::test_unknown",
                                "current_behavior": (
                                    "The cited test is not present in the supplied context."
                                ),
                                "gap": "The missing test cannot be assessed from this evidence.",
                                "why_it_matters": (
                                    "Unsupported citations make the report unverifiable."
                                ),
                                "recommended_test": (
                                    "Inspect the supplied files before proposing a test."
                                ),
                                "expected_assertion": (
                                    "Use an assertion grounded in an observed behavior."
                                ),
                            }
                        ],
                    }
                },
                "confidence": 0.8,
                "requires_approval": False,
            },
        ),
    )
    context = AgentContext(test_sources={"tests/test_quality.py": "def test_source(): assert True"})

    decision = TestQualityAgent().reason(context, TestQualityAgent().observe(context))

    findings = decision.arguments["quality_report"].get("findings", [])
    assert findings
    assert all("tests/missing.py" not in finding["location"] for finding in findings)


def test_test_quality_rejects_generic_report_with_test_sources(monkeypatch) -> None:
    monkeypatch.setattr(
        "agents.base.LLMClient.complete",
        lambda *_args: LLMResponse(
            success=True,
            parsed={
                "action": "report_test_quality",
                "reason": "The tests are generally good.",
                "arguments": {
                    "quality_report": {
                        "rating": "Good",
                        "score": 85,
                        "assertions": "The tests assert behavior in several important paths.",
                        "behavior_coverage": "The suite covers core behavior but may miss edges.",
                        "isolation_mocking": "The supplied tests use deterministic local inputs.",
                        "reliability": "The suite is reliable based on the supplied results.",
                        "findings": [],
                        "weak_tests": [],
                        "missing_behaviors": [],
                        "recommendations": ["Add more test cases for different scenarios."],
                    }
                },
                "confidence": 0.8,
                "requires_approval": False,
            },
        ),
    )
    context = AgentContext(test_sources={"tests/test_payment.py": "def test_charge(): assert True"})

    decision = TestQualityAgent().reason(context, TestQualityAgent().observe(context))

    assert decision.arguments["quality_report"]["rating"] == "Needs improvement"
    assert "tests/test_payment.py" in decision.arguments["quality_report"]["weak_tests"][0]


def test_test_quality_rejects_generic_finding_recommendation() -> None:
    report = {
        "rating": "Needs improvement",
        "score": 60,
        "assertions": "The supplied tests include some meaningful behavior assertions.",
        "behavior_coverage": "The supplied tests leave important visible branches untested.",
        "isolation_mocking": "The supplied test file does not show external dependency isolation.",
        "reliability": "The supplied local tests are deterministic but incomplete.",
        "findings": [
            {
                "category": "assertions",
                "location": "tests/test_payment.py::test_charge",
                "current_behavior": "The test exercises the charge path with one input.",
                "gap": "A boundary input is not asserted in the supplied test.",
                "why_it_matters": "The boundary can regress without a failing test.",
                "recommended_test": "Add more tests for edge cases.",
                "expected_assertion": "Assert the expected payment result.",
            },
            {
                "category": "behavior_coverage",
                "location": "tests/test_payment.py::test_charge",
                "current_behavior": "The test checks one successful behavior path.",
                "gap": "The error branch is not exercised by the supplied test.",
                "why_it_matters": "Error handling can regress independently of success.",
                "recommended_test": "Add a named error test in tests/test_payment.py.",
                "expected_assertion": "Assert the expected exception.",
            },
            {
                "category": "isolation_mocking",
                "location": "tests/test_payment.py::test_charge",
                "current_behavior": "The test uses a local payment input.",
                "gap": "The dependency boundary is not visible in the test.",
                "why_it_matters": "Uncontrolled dependencies can make the test flaky.",
                "recommended_test": "Add a mock fixture in tests/test_payment.py.",
                "expected_assertion": "Assert the result from the mock response.",
            },
        ],
    }

    with pytest.raises(ValueError, match="too generic"):
        TestQualityAgent._validate_finding_evidence(
            report,
            AgentContext(test_sources={"tests/test_payment.py": "def test_charge(): pass"}),
            {},
        )


def test_llm_client_uses_defaults_for_empty_environment(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)

    client = LLMClient()

    assert client.base_url == "https://api.openai.com/v1"
    assert client.model == "gpt-4o-mini"
    assert client.timeout == 30.0


def test_llm_client_uses_configured_timeout(monkeypatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "300")

    client = LLMClient()

    assert client.timeout == 300.0


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