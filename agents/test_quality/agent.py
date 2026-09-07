"""Test Quality Agent.

Reviews test source semantically in addition to coverage. It looks for weak
assertions, tests that do not exercise the intended behavior, missing mocks or
isolation, and untested behavior. It can also generate candidate tests for
known gaps; it never merges or silently commits anything itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agents.base import AgentContext, AgentDecision, BaseAgent
from llm.client import LLMClient
from llm.models import LLMMessage, LLMRequest
from tools.testing.pytest_tools import run_pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Heuristics mapping a keyword that might appear in a diff to a recommended
#: test description and a ready-to-run candidate pytest test. This keeps the
#: demo deterministic and fast; a real implementation could ask the LLM to
#: draft additional candidates for review.
_KNOWN_GAPS: dict[str, tuple[str, str]] = {
    "amount == 0": (
        "zero-dollar transaction",
        """
def test_validate_payment_approves_zero_amount():
    from app.models import PaymentMethod, PaymentMethodType
    from app.services.payment_service import PaymentService

    service = PaymentService()
    method = PaymentMethod(type=PaymentMethodType.CREDIT_CARD, token="tok_123")
    result = service.validate_payment(method, 0)
    assert result.approved is True
""",
    ),
    "GIFT_CARD_MAX_AMOUNT": (
        "gift card amount exceeding the configured limit",
        """
def test_validate_payment_rejects_gift_card_over_limit():
    from app.models import PaymentMethod, PaymentMethodType
    from app.services.payment_service import PaymentService

    service = PaymentService()
    method = PaymentMethod(type=PaymentMethodType.GIFT_CARD, token="tok_123")
    result = service.validate_payment(method, 501)
    assert result.approved is False
""",
    ),
}


class TestQualityReport(BaseModel):
    """Structured quality findings returned by the dedicated quality request."""

    rating: str
    score: int
    assertions: str
    behavior_coverage: str
    isolation_mocking: str
    reliability: str
    weak_tests: list[str] = Field(default_factory=list)
    missing_behaviors: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class TestQualityAgent(BaseAgent):
    """Analyzes diff/coverage and proposes or validates missing tests."""

    name = "test_quality_agent"

    @staticmethod
    def _request_quality_report(
        context: AgentContext, observation: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Request the detailed report when the primary decision omitted it."""
        request = LLMRequest(
            messages=[
                LLMMessage(
                    role="system",
                    content=(
                        "Return only a JSON object matching the supplied schema. "
                        "Assess test quality from the authoritative pytest results, "
                        "coverage, changed files, diff, and test sources. Do not invent "
                        "test results or coverage."
                    ),
                ),
                LLMMessage(
                    role="user",
                    content=(
                        "Provide detailed quality findings for this repository data. "
                        "Every field is required; use an empty list when there are no "
                        "findings.\n\n"
                        f"Agent context:\n{context.model_dump_json()}\n\n"
                        f"Observation:\n{observation}"
                    ),
                ),
            ],
            response_schema=TestQualityReport.model_json_schema(),
            temperature=0.1,
            max_tokens=1200,
        )
        response = LLMClient().complete(request)
        if not response.success or response.parsed is None:
            return None
        try:
            return TestQualityReport.model_validate(response.parsed).model_dump()
        except ValueError:
            return None

    @staticmethod
    def _render_quality_report(decision: AgentDecision) -> AgentDecision:
        """Make the structured quality report visible in logs and PR comments."""
        report = decision.arguments.get("quality_report")
        if not isinstance(report, dict):
            return decision
        sections = [
            f"Quality rating: {report.get('rating', 'Unknown')} "
            f"({report.get('score', 'n/a')}/100)",
            f"Assertions: {report.get('assertions', 'Not assessed')}",
            f"Behavior coverage: {report.get('behavior_coverage', 'Not assessed')}",
            f"Isolation/mocking: {report.get('isolation_mocking', 'Not assessed')}",
            f"Reliability: {report.get('reliability', 'Not assessed')}",
        ]
        for key, label in (
            ("weak_tests", "Weak tests"),
            ("missing_behaviors", "Missing behaviors"),
            ("recommendations", "Recommendations"),
        ):
            values = report.get(key, [])
            if isinstance(values, list) and values:
                sections.append(f"{label}:\n" + "\n".join(f"- {value}" for value in values))
        decision.reason = decision.reason + "\n\n" + "\n".join(sections)
        return decision

    def observe(self, context: AgentContext) -> dict[str, Any]:
        gaps = [
            (keyword, desc, code)
            for keyword, (desc, code) in _KNOWN_GAPS.items()
            if keyword in context.diff
        ]
        return {
            "changed_files": context.changed_files,
            "test_results": context.test_results,
            "coverage_before": context.coverage.get("before"),
            "coverage_after": context.coverage.get("after"),
            "test_sources": context.test_sources,
            "gaps": gaps,
        }

    def reason(self, context: AgentContext, observation: dict[str, Any]) -> AgentDecision:
        gaps = observation.get("gaps", [])
        if not gaps:
            fallback = AgentDecision(
                action="no_action",
                reason=(
                    "No known under-tested branches detected in this diff. "
                    "Review the test-quality report for assertion strength, "
                    "behavior coverage, mocking, and isolation findings."
                ),
                confidence=0.4,
                requires_approval=False,
            )
        else:
            descriptions = ", ".join(desc for _, desc, _ in gaps)
            fallback = AgentDecision(
                action="propose_tests",
                reason=(
                    f"Detected potentially under-tested behavior: {descriptions}. "
                    "Review the test-quality report for assertion strength, "
                    "behavior coverage, mocking, and isolation findings."
                ),
                tool="execute_tests",
                arguments={"candidate_count": len(gaps)},
                confidence=0.75,
                requires_approval=True,
            )
        decision = self.reason_with_llm(
            context, observation, fallback, "test_quality.md", ["execute_tests"]
        )
        if not isinstance(decision.arguments.get("quality_report"), dict):
            quality_report = self._request_quality_report(context, observation)
            if quality_report is not None:
                decision.arguments["quality_report"] = quality_report
        decision = self._render_quality_report(decision)
        results = context.test_results
        tests = results.get("tests") if isinstance(results, dict) else None
        failures = results.get("failures_count", 0) if isinstance(results, dict) else 0
        if isinstance(tests, int) and tests > 0:
            decision.reason += (
                f"\n\nAuthoritative pytest results: {tests} tests executed, "
                f"{failures} failure(s)."
            )
        else:
            decision.reason += "\n\nAuthoritative pytest results: unavailable."
        return decision

    def act(self, context: AgentContext, decision: AgentDecision) -> dict[str, Any]:
        if decision.tool != "execute_tests":
            return {}
        gaps = [
            (keyword, desc, code)
            for keyword, (desc, code) in _KNOWN_GAPS.items()
            if keyword in context.diff
        ]
        generated_path = REPO_ROOT / "tests" / "test_agent_generated_candidates.py"
        header = '"""Auto-generated candidate tests from the Test Quality Agent."""\n'
        body = "\n".join(code for _, _, code in gaps)
        generated_path.write_text(header + body, encoding="utf-8")
        try:
            run_result = run_pytest([str(generated_path)], REPO_ROOT)
        finally:
            generated_path.unlink(missing_ok=True)
        return {
            "recommended_tests": [desc for _, desc, _ in gaps],
            "generated_count": len(gaps),
            "passed": run_result.passed,
            "failed": run_result.failed,
            "all_passed": run_result.all_passed,
        }

    def validate(
        self, context: AgentContext, decision: AgentDecision, tool_result: dict[str, Any]
    ) -> dict[str, Any]:
        if decision.tool != "execute_tests":
            return {"skipped": True}
        return {
            "generated_tests_passed": tool_result.get("all_passed", False),
            "passed": tool_result.get("passed", 0),
            "failed": tool_result.get("failed", 0),
        }
