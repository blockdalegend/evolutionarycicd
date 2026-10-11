"""Map changed Python modules to tests using deterministic import analysis."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from agents.base import AgentContext, AgentDecision, BaseAgent
from agents.issue_proposals import IssueProposal

_SAFE_REPOSITORY_PATH = re.compile(r"[A-Za-z0-9_./-]+")


def _safe_repository_path(path: str) -> bool:
    candidate = Path(path)
    return (
        not candidate.is_absolute()
        and ".." not in candidate.parts
        and _SAFE_REPOSITORY_PATH.fullmatch(path) is not None
    )


def _imports(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


class TestImpactAgent(BaseAgent):
    """Recommend test relationships; never decides to skip mandatory checks."""

    name = "test_impact_agent"

    def observe(self, context: AgentContext) -> dict[str, Any]:
        changed = [
            path
            for path in context.changed_files
            if path.endswith(".py")
            and not path.startswith("tests/")
            and _safe_repository_path(path)
        ]
        test_sources = dict(context.test_sources)
        test_sources.update(
            {
                path: source
                for path, source in context.source_files.items()
                if path.startswith("tests/") and _safe_repository_path(path)
            }
        )
        module_imports = {
            Path(path).with_suffix("").as_posix().replace("/", "."): _imports(source)
            for path, source in context.source_files.items()
            if path.endswith(".py") and not path.startswith("tests/")
        }
        relationships: list[dict[str, Any]] = []
        missing: list[str] = []
        potentially_affected_integration: set[str] = set()
        for source_path in changed:
            module = Path(source_path).with_suffix("").as_posix().replace("/", ".")
            module_stem = Path(source_path).stem
            related: list[str] = []
            for test_path, test_source in sorted(test_sources.items()):
                imported = _imports(test_source)
                filename_match = module_stem in Path(test_path).stem
                reachable = set(imported)
                pending = list(imported)
                while pending:
                    candidate = pending.pop()
                    for dependency in module_imports.get(candidate, set()):
                        if dependency not in reachable:
                            reachable.add(dependency)
                            pending.append(dependency)
                direct_import = module in imported
                import_match = module in reachable
                if filename_match or import_match:
                    related.append(test_path)
                    if "integration" in test_path.lower() or (import_match and not direct_import):
                        potentially_affected_integration.add(test_path)
            relationships.append(
                {
                    "source_file": source_path,
                    "module": module,
                    "direct_tests": related,
                    "relationship_evidence": (
                        "filename, direct AST import, or transitive AST import"
                    ),
                }
            )
            if not related:
                missing.append(source_path)
        integration_tests = sorted(
            path
            for path in test_sources
            if "integration" in path.lower() and _safe_repository_path(path)
        )
        return {
            "relationships": relationships,
            "unmapped_source_files": missing,
            "integration_tests_available": integration_tests,
            "potentially_affected_integration_tests": sorted(potentially_affected_integration),
            "mandatory_checks_skipped": False,
        }

    def reason(self, context: AgentContext, observation: dict[str, Any]) -> AgentDecision:
        missing = observation["unmapped_source_files"]
        if not missing:
            return AgentDecision(
                action="no_action",
                reason=(
                    "Every changed Python source file has at least one deterministic "
                    "test relationship."
                ),
                arguments={"relationships": observation["relationships"]},
                confidence=0.8,
                requires_approval=False,
            )
        confidence = min(0.92, 0.75 + 0.03 * len(missing))
        reason = f"No test mapping was found for {len(missing)} changed Python source file(s)."
        proposal = IssueProposal(
            title=f"[Test Impact] Improve test mapping for {Path(missing[0]).stem}",
            summary=reason,
            problem=(
                "Deterministic AST import and filename analysis found changed source modules "
                "with no directly associated test file. This is advisory and does not "
                "change CI selection."
            ),
            evidence=[f"Unmapped source file: {path}" for path in missing]
            + [
                f"Changed files analyzed: {len(observation['relationships'])}",
                f"Integration test files available: {observation['integration_tests_available']}",
                "No required CI checks were skipped or altered.",
            ],
            affected_files=missing,
            requested_change=(
                "Add focused tests or an explicit source-to-test mapping for each listed module. "
                "Keep all mandatory CI test jobs enabled; mapping changes are advisory only."
            ),
            acceptance_criteria=[
                "Each listed changed module has a deterministic relationship to one or more tests.",
                "The relationship is covered by a passing test-selection validation.",
                "No mandatory CI check is skipped based on an LLM or heuristic recommendation.",
            ],
            validation_commands=["pytest -q", "pytest --collect-only -q"],
            labels=["agent-recommendation"],
            source_agent=self.name,
            confidence=confidence,
            deduplication_key="modules:" + ",".join(sorted(missing)),
            assign_to_copilot=False,
            finding_type="unmapped_changed_modules",
        )
        return AgentDecision(
            action="report_test_mapping_gap",
            reason=reason,
            tool="create_issue",
            arguments={
                "finding": observation,
                "issue_proposal": proposal.model_dump(mode="json"),
                "advisory_only": True,
            },
            confidence=confidence,
            requires_approval=True,
        )

    def act(self, context: AgentContext, decision: AgentDecision) -> dict[str, Any]:
        return {
            "relationships": decision.arguments.get("finding", {}).get("relationships", []),
            "published": False,
        }

    def validate(
        self, context: AgentContext, decision: AgentDecision, tool_result: dict[str, Any]
    ) -> dict[str, Any]:
        return {"advisory_only": True, "mandatory_checks_skipped": False}
