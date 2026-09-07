"""Test Quality Agent.

Reviews test source semantically in addition to coverage. It looks for weak
assertions, tests that do not exercise the intended behavior, missing mocks or
isolation, and untested behavior. It can also generate candidate tests for
known gaps; it never merges or silently commits anything itself.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from agents.base import AgentContext, AgentDecision, BaseAgent
from llm.client import LLMClient
from llm.models import LLMMessage, LLMRequest
from telemetry.logger import get_logger
from tools.testing.pytest_tools import run_pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
logger = get_logger(__name__)

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


class TestQualityChange(BaseModel):
    """One complete test-file change proposed by the quality model."""

    path: str = Field(pattern=r"^tests/[A-Za-z0-9_./-]+\.py$")
    content: str = Field(min_length=40)
    rationale: str = Field(min_length=20)
    finding_locations: list[str] = Field(min_length=1, max_length=3)


class TestQualityReport(BaseModel):
    """Structured quality findings returned by the dedicated quality request."""

    rating: Literal["Excellent", "Good", "Needs improvement", "Poor"]
    score: int = Field(ge=0, le=100)
    assertions: str = Field(min_length=20)
    behavior_coverage: str = Field(min_length=20)
    isolation_mocking: str = Field(min_length=20)
    reliability: str = Field(min_length=20)
    findings: list[TestQualityFinding] = Field(default_factory=list, max_length=5)
    weak_tests: list[str] = Field(default_factory=list, max_length=5)
    missing_behaviors: list[str] = Field(default_factory=list, max_length=5)
    recommendations: list[str] = Field(default_factory=list, max_length=5)
    proposed_changes: list[TestQualityChange] = Field(default_factory=list, max_length=3)


class TestQualityFinding(BaseModel):
    """One evidence-backed issue and its concrete remediation."""

    category: Literal["assertions", "behavior_coverage", "isolation_mocking", "reliability"]
    location: str = Field(min_length=3)
    current_behavior: str = Field(min_length=20)
    gap: str = Field(min_length=20)
    why_it_matters: str = Field(min_length=20)
    recommended_test: str = Field(min_length=20)
    expected_assertion: str = Field(min_length=10)


class TestQualityAgent(BaseAgent):
    """Analyzes diff/coverage and proposes or validates missing tests."""

    @staticmethod
    def _normalize_quality_report(parsed: dict[str, Any]) -> dict[str, Any]:
        """Map common provider synonyms to the report contract."""
        aliases = {
            "rating": ("overall_rating",),
            "score": ("quality_score",),
            "assertions": ("assertion_analysis",),
            "behavior_coverage": ("coverage_analysis",),
            "isolation_mocking": ("mocking_analysis", "isolation_analysis"),
            "reliability": ("reliability_analysis",),
        }
        normalized = dict(parsed)
        for field, alternatives in aliases.items():
            if field not in normalized:
                for alternative in alternatives:
                    if alternative in parsed:
                        normalized[field] = parsed[alternative]
                        break
        return normalized

    @staticmethod
    def _validate_quality_report(parsed: dict[str, Any]) -> dict[str, Any]:
        """Reject reports that are structurally valid but semantically useless."""
        report = TestQualityReport.model_validate(
            TestQualityAgent._normalize_quality_report(parsed)
        )
        expected_rating = (
            "Excellent"
            if report.score >= 90
            else "Good"
            if report.score >= 75
            else "Needs improvement"
            if report.score >= 50
            else "Poor"
        )
        if report.rating != expected_rating:
            raise ValueError(
                f"rating {report.rating!r} does not match score {report.score}; "
                f"expected {expected_rating!r}"
            )
        for field in (
            "assertions",
            "behavior_coverage",
            "isolation_mocking",
            "reliability",
        ):
            value = getattr(report, field).strip()
            if value.isdigit() or value.lower() in {"testing", "unknown", "n/a"}:
                raise ValueError(f"{field} is not a substantive assessment")
        for change in report.proposed_changes:
            change_path = Path(change.path)
            if change_path.is_absolute() or ".." in change_path.parts:
                raise ValueError(f"proposed change escapes the tests directory: {change.path}")
            if "```" in change.content:
                raise ValueError(f"proposed change contains a markdown fence: {change.path}")
            if not change.content.lstrip().startswith(("from ", "import ", '"""', "#")):
                raise ValueError(f"proposed change is not a Python source file: {change.path}")
        return report.model_dump()

    @staticmethod
    def _validate_finding_evidence(
        report: dict[str, Any], context: AgentContext, observation: dict[str, Any]
    ) -> dict[str, Any]:
        """Keep model detail only when its cited location exists in the evidence."""
        validated = TestQualityAgent._validate_quality_report(report)
        known_files = set(context.changed_files) | set(context.test_sources)
        findings = validated.get("findings", [])
        if (context.test_sources or observation.get("gaps")) and not findings:
            raise ValueError(
                "detailed findings are required when test sources or diff gaps are supplied"
            )
        if context.test_sources or observation.get("gaps"):
            categories = {finding["category"] for finding in findings}
            required_categories = {"assertions", "behavior_coverage", "isolation_mocking"}
            if not required_categories.issubset(categories):
                missing = ", ".join(sorted(required_categories - categories))
                raise ValueError(f"findings are missing category detail: {missing}")
        evidence = json.dumps(
            {"context": context.model_dump(), "observation": observation},
            default=str,
        )
        for change in validated.get("proposed_changes", []):
            for location in change["finding_locations"]:
                if location not in evidence:
                    raise ValueError(
                        f"proposed change location is not in supplied evidence: {location}"
                    )
        for finding in findings:
            location = finding["location"].strip()
            location_parts = [part.strip() for part in location.split("::") if part.strip()]
            recommended_test = finding["recommended_test"].lower()
            generic_phrases = (
                "add more tests",
                "cover edge cases",
                "improve organization",
                "improve readability",
            )
            if any(phrase in recommended_test for phrase in generic_phrases):
                raise ValueError(
                    f"finding recommendation is too generic: {location}"
                )
            if location_parts[0].lower() not in recommended_test:
                raise ValueError(
                    "finding recommendation must name its cited file: "
                    f"{location}"
                )
            if (
                not location_parts
                or location_parts[0] not in known_files
                or not all(part in evidence for part in location_parts)
            ):
                raise ValueError(
                    f"finding location is not an exact supplied file/test: {location}"
                )
        return validated

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
                        "The tool must be null or execute_tests; never select "
                        "comment_pull_request or any other tool. "
                        "Assess test quality from the authoritative pytest results, "
                        "coverage, changed files, diff, and test sources. Do not invent "
                        "test results, coverage, identifiers, or behaviors. Every "
                        "finding must cite an exact test, file, branch, or behavior "
                        "from the supplied context. Do not give generic advice. "
                        "Treat passing pytest as execution evidence, not proof of "
                        "completeness. Score 0 only when tests provide no meaningful "
                        "evidence; use 50-74 for meaningful tests with important gaps. "
                        "Keep each narrative field under 60 words and each list to "
                        "at most 3 high-signal items. For every finding, explain the "
                        "current behavior, the concrete gap, why it matters, the exact "
                        "test or code change to make, and the expected assertion. "
                        "Include at least one finding each for assertions, behavior_coverage, "
                        "and isolation_mocking. If a category has no defect, cite the exact "
                        "test file and explain what was checked and why no change is needed. "
                        "The recommended_test must repeat the cited file path and name a "
                        "specific test function to add or change. Never write 'add more tests', "
                        "'cover edge cases', or 'improve organization' without naming the file, "
                        "function, input, expected result, and assertion. If you recommend a "
                        "fix, include it in proposed_changes as complete Python test-file "
                        "content and link it to the finding location."
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
            max_tokens=6000,
        )
        response = LLMClient().complete(request)
        parsed = response.parsed
        if parsed is None and response.content:
            content = response.content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            try:
                parsed = json.loads(content[content.find("{") : content.rfind("}") + 1])
            except (json.JSONDecodeError, ValueError):
                parsed = None
        if not response.success and parsed is None:
            logger.warning("Detailed test-quality LLM report unavailable: %s", response.error)
            return None
        try:
            if not isinstance(parsed, dict):
                return None
            return TestQualityAgent._validate_finding_evidence(parsed, context, observation)
        except ValueError as exc:
            logger.warning("Detailed test-quality LLM report did not match schema: %s", exc)
            return None

    @staticmethod
    def _fallback_quality_report(
        context: AgentContext, observation: dict[str, Any]
    ) -> dict[str, Any]:
        """Provide transparent deterministic findings when the LLM report fails."""
        weak_tests = [
            f"{path} contains an unconditional assertion (assert True)."
            for path, source in context.test_sources.items()
            if "assert True" in source
        ]
        gaps = observation.get("gaps", [])
        missing_behaviors = [description for _, description, _ in gaps]
        recommendations = [
            f"Add a focused test for {behavior}, asserting the expected outcome."
            for behavior in missing_behaviors
        ]
        if not recommendations:
            recommendations.append(
                "Review boundary and error-path behavior in the changed code, then add "
                "tests that assert returned values or raised exceptions."
            )
        tests = context.test_results.get("tests", "unknown")
        failures = context.test_results.get("failures_count", "unknown")
        assertion_count = sum(source.count("assert ") for source in context.test_sources.values())
        weak_test_count = len(weak_tests)
        known_gap_text = (
            ", ".join(missing_behaviors)
            if missing_behaviors
            else "no known diff-linked gaps detected"
        )
        source_files = ", ".join(context.test_sources) or "the supplied changed files"
        fallback_location = next(iter(context.test_sources), "the supplied changed files")
        fallback_findings = [
            {
                "category": "assertions",
                "location": fallback_location,
                "current_behavior": (
                    f"The fallback inspected {source_files}; assertion semantics were not "
                    "analyzed by the LLM."
                ),
                "gap": "A semantic assertion-level gap could not be determined without the LLM.",
                "why_it_matters": (
                    "A passing test count does not prove that expected behavior is asserted."
                ),
                "recommended_test": (
                    "Have the LLM review the cited test file and identify the exact "
                    "assertion to strengthen."
                ),
                "expected_assertion": (
                    "Assert the expected returned value or exception for the cited behavior."
                ),
            },
            {
                "category": "behavior_coverage",
                "location": fallback_location,
                "current_behavior": (
                    f"The fallback reviewed {source_files}, but did not perform semantic "
                    "branch analysis."
                ),
                "gap": (
                    "The exact missing behavior requires a model review of the supplied "
                    "diff and tests."
                ),
                "why_it_matters": (
                    "Unexercised boundary and error paths can regress while pytest remains green."
                ),
                "recommended_test": (
                    "Have the LLM map visible branches in the changed file to named tests "
                    "in the cited test file."
                ),
                "expected_assertion": (
                    "Assert the documented outcome for each identified boundary or error branch."
                ),
            },
            {
                "category": "isolation_mocking",
                "location": fallback_location,
                "current_behavior": (
                    f"The fallback inspected {source_files}; dependency isolation was not "
                    "assessed semantically."
                ),
                "gap": (
                    "The available fallback cannot determine whether external calls are isolated."
                ),
                "why_it_matters": (
                    "Unisolated network, filesystem, or shared-state calls can make tests flaky."
                ),
                "recommended_test": (
                    "Have the LLM identify external dependencies in the cited file and name "
                    "the mock or fake required."
                ),
                "expected_assertion": (
                    "Assert the behavior using a controlled mock response without making a "
                    "real external call."
                ),
            },
        ]
        return {
            "rating": "Needs improvement" if weak_tests or missing_behaviors else "Good",
            "score": 60 if weak_tests or missing_behaviors else 75,
            "assertions": (
                f"Reviewed {len(context.test_sources)} test source file(s) and found "
                f"{assertion_count} assertion(s); {weak_test_count} unconditional assertion(s) "
                f"were detected. Pytest reported {tests} test(s) and {failures} failure(s)."
            ),
            "behavior_coverage": (
                f"Pytest execution is authoritative, but semantic coverage was not assessed; "
                f"diff-linked review identified {known_gap_text}."
            ),
            "isolation_mocking": (
                "Deterministic fallback did not identify external-system isolation from the "
                "available test source; review mocks and fakes where dependencies are used."
            ),
            "reliability": (
                "Pytest completed with the reported result; deterministic fallback did not "
                "execute additional reliability or flakiness analysis."
            ),
            "findings": fallback_findings,
            "weak_tests": weak_tests,
            "missing_behaviors": missing_behaviors,
            "recommendations": recommendations,
        }

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
        findings = report.get("findings", [])
        if isinstance(findings, list) and findings:
            rendered_findings = []
            for category in ("assertions", "behavior_coverage", "isolation_mocking", "reliability"):
                for finding in findings:
                    if finding.get("category") != category:
                        continue
                    rendered_findings.append(
                        "- ["
                        + category
                        + "] "
                        + finding["location"]
                        + ": Current: "
                        + finding["current_behavior"]
                        + " Gap: "
                        + finding["gap"]
                        + " Why: "
                        + finding["why_it_matters"]
                        + " Fix: "
                        + finding["recommended_test"]
                        + " Assert: "
                        + finding["expected_assertion"]
                    )
            sections.append("Detailed findings:\n" + "\n".join(rendered_findings))
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
            context,
            observation,
            fallback,
            "test_quality.md",
            ["execute_tests", "create_pull_request"],
            preserve_argument_keys=("quality_report",),
        )
        quality_report = decision.arguments.get("quality_report")
        try:
            if isinstance(quality_report, dict):
                decision.arguments["quality_report"] = self._validate_quality_report(
                    quality_report
                )
            else:
                raise ValueError("quality_report is missing")
        except (TypeError, ValueError) as exc:
            logger.warning("Ignoring invalid primary test-quality report: %s", exc)
            quality_report = self._request_quality_report(context, observation)
            decision.arguments["quality_report"] = (
                quality_report
                if quality_report is not None
                else self._fallback_quality_report(context, observation)
            )
        else:
            try:
                decision.arguments["quality_report"] = self._validate_finding_evidence(
                    decision.arguments["quality_report"], context, observation
                )
            except ValueError as exc:
                logger.warning("Ignoring unsupported primary test-quality findings: %s", exc)
                report = self._request_quality_report(context, observation)
                decision.arguments["quality_report"] = (
                    report
                    if report is not None
                    else self._fallback_quality_report(context, observation)
                )
        proposed_changes = decision.arguments["quality_report"].get("proposed_changes", [])
        if proposed_changes:
            decision.action = "propose_fix_pull_request"
            decision.tool = "create_pull_request"
            decision.requires_approval = True
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
        report = decision.arguments.get("quality_report", {})
        proposed_changes = report.get("proposed_changes", []) if isinstance(report, dict) else []
        if decision.tool == "create_pull_request" and proposed_changes:
            return self._create_fix_pull_request(context, decision, proposed_changes)
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

    def _create_fix_pull_request(
        self,
        context: AgentContext,
        decision: AgentDecision,
        proposed_changes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Apply model-authored test changes and open a reviewable pull request."""
        from tools.github.client import GitHubClient

        client = GitHubClient()
        files = {change["path"]: change["content"] for change in proposed_changes}
        if os.environ.get("AGENT_ALLOW_FIX_PR", "false").lower() not in {"1", "true", "yes"}:
            logger.info(
                "Test Quality proposed fixes require AGENT_ALLOW_FIX_PR=true; keeping PR in "
                "dry-run mode"
            )
            return {
                "created": False,
                "dry_run": True,
                "approval_required": True,
                "files": sorted(files),
            }
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temporary_directory:
            temporary_root = Path(temporary_directory)
            candidate_paths = []
            for path, content in files.items():
                candidate_path = temporary_root / Path(path)
                candidate_path.parent.mkdir(parents=True, exist_ok=True)
                candidate_path.write_text(content, encoding="utf-8")
                candidate_paths.append(str(candidate_path))
            run_result = run_pytest(candidate_paths, REPO_ROOT)
        if not run_result.all_passed:
            return {
                "created": False,
                "error": "proposed tests did not pass",
                "passed": run_result.passed,
                "failed": run_result.failed,
            }
        if client.dry_run:
            logger.info(
                "[dry-run] would create Test Quality fix PR",
                extra={"extra_fields": {"files": [change["path"] for change in proposed_changes]}},
            )
            return {"dry_run": True, "files": [change["path"] for change in proposed_changes]}

        if not context.repository:
            return {"created": False, "error": "repository is required to create a PR"}
        branch = f"agent/test-quality-{context.commit_sha or 'fix'}"[:60]
        result = client.create_pull_request_with_files(
            branch_name=branch,
            base_branch="main",
            title="Test Quality Agent: add missing coverage",
            body=decision.reason,
            files=files,
            repository=context.repository,
        )
        return result

    def validate(
        self, context: AgentContext, decision: AgentDecision, tool_result: dict[str, Any]
    ) -> dict[str, Any]:
        if decision.tool != "execute_tests":
            return {
                "skipped": decision.tool != "create_pull_request",
                "pull_request_created": tool_result.get("created", False),
                "pull_request_url": tool_result.get("url"),
            }
        return {
            "generated_tests_passed": tool_result.get("all_passed", False),
            "passed": tool_result.get("passed", 0),
            "failed": tool_result.get("failed", 0),
        }
