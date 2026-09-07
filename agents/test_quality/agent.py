"""Test Quality Agent.

Looks at what changed, the PR diff, existing test results, and coverage, and
identifies branches/behavior that appear insufficiently tested. It can
optionally generate *candidate* pytest tests, execute them, and report
whether they passed -- it never merges or silently commits anything itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.base import AgentContext, AgentDecision, BaseAgent
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


class TestQualityAgent(BaseAgent):
    """Analyzes diff/coverage and proposes or validates missing tests."""

    name = "test_quality_agent"

    def observe(self, context: AgentContext) -> dict[str, Any]:
        gaps = [
            (keyword, desc, code)
            for keyword, (desc, code) in _KNOWN_GAPS.items()
            if keyword in context.diff
        ]
        return {
            "changed_files": context.changed_files,
            "coverage_before": context.coverage.get("before"),
            "coverage_after": context.coverage.get("after"),
            "gaps": gaps,
        }

    def reason(self, context: AgentContext, observation: dict[str, Any]) -> AgentDecision:
        gaps = observation.get("gaps", [])
        if not gaps:
            fallback = AgentDecision(
                action="no_action",
                reason="No known under-tested branches detected in this diff.",
                confidence=0.4,
                requires_approval=False,
            )
        else:
            descriptions = ", ".join(desc for _, desc, _ in gaps)
            fallback = AgentDecision(
                action="propose_tests",
                reason=f"Detected potentially under-tested behavior: {descriptions}.",
                tool="execute_tests",
                arguments={"candidate_count": len(gaps)},
                confidence=0.75,
                requires_approval=True,
            )
        return self.reason_with_llm(
            context, observation, fallback, "test_quality.md", ["execute_tests"]
        )

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
