"""Identify new configuration and public API declarations absent from docs."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from agents.base import AgentContext, AgentDecision, BaseAgent
from agents.issue_proposals import IssueProposal

_ENV_REFERENCE = re.compile(
    r"(?:os\.)?environ(?:\.get)?\s*(?:\[\s*|\(\s*)['\"]([A-Z][A-Z0-9_]{2,})['\"]"
    r"|getenv\s*\(\s*['\"]([A-Z][A-Z0-9_]{2,})['\"]"
)
_ARGUMENT = re.compile(r"add_argument\s*\(\s*['\"](--[a-z0-9][a-z0-9-]*)['\"]")


class DocumentationDriftAgent(BaseAgent):
    """Detect specific new configuration/API declarations lacking documentation."""

    name = "documentation_drift_agent"

    @staticmethod
    def _public_functions(source: str) -> set[str]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return set()
        return {
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and not node.name.startswith("_")
        }

    @staticmethod
    def _documentation(context: AgentContext) -> str:
        documents = {
            path: text
            for path, text in context.source_files.items()
            if Path(path).name in {"README.md", ".env.example"} or path.startswith("docs/")
        }
        root = Path(__file__).resolve().parents[2]
        for path in [
            root / "README.md",
            root / ".env.example",
            *sorted((root / "docs").glob("*.md")),
        ]:
            relative = path.relative_to(root).as_posix()
            if relative in documents:
                continue
            try:
                documents[relative] = path.read_text(encoding="utf-8")
            except OSError:
                continue
        return "\n".join(documents.values())

    def observe(self, context: AgentContext) -> dict[str, Any]:
        additions = [
            line[1:].strip()
            for line in context.diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        introduced_env = {
            match.group(1) or match.group(2)
            for line in additions
            for match in _ENV_REFERENCE.finditer(line)
        }
        introduced_flags = {
            match.group(1) for line in additions for match in _ARGUMENT.finditer(line)
        }
        docs_text = self._documentation(context)
        drift: list[dict[str, str]] = []
        for name in sorted(introduced_env):
            if name not in docs_text:
                source = next(
                    (path for path, content in context.source_files.items() if name in content),
                    next(
                        (path for path in context.changed_files if path.endswith(".py")),
                        "repository",
                    ),
                )
                drift.append(
                    {
                        "identifier": name,
                        "kind": "environment_variable",
                        "source_file": source,
                        "missing_from": ".env.example and repository documentation",
                    }
                )
        root = Path(__file__).resolve().parents[2]
        for path, source in context.source_files.items():
            if not path.startswith("app/") or not path.endswith(".py"):
                continue
            base_path = root / path
            try:
                previous_source = base_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                previous_source = ""
            added_functions = self._public_functions(source) - self._public_functions(
                previous_source
            )
            for function_name in sorted(added_functions):
                if function_name not in docs_text:
                    drift.append(
                        {
                            "identifier": function_name,
                            "kind": "public_api",
                            "source_file": path,
                            "missing_from": "repository API documentation",
                        }
                    )
        for flag in sorted(introduced_flags):
            if flag not in docs_text:
                source = next(
                    (path for path in context.changed_files if path.endswith(".py")), "repository"
                )
                drift.append(
                    {
                        "identifier": flag,
                        "kind": "cli_option",
                        "source_file": source,
                        "missing_from": "repository CLI documentation",
                    }
                )
        return {"drift": drift, "declarations_checked": len(introduced_env) + len(introduced_flags)}

    def reason(self, context: AgentContext, observation: dict[str, Any]) -> AgentDecision:
        findings = observation["drift"]
        if not findings:
            return AgentDecision(
                action="no_action",
                reason=(
                    "No specific new configuration or CLI declaration was found to be "
                    "undocumented."
                ),
                confidence=0.8,
                requires_approval=False,
            )
        finding = findings[0]
        identifier = finding["identifier"]
        if finding["kind"] == "environment_variable":
            title = f"[Documentation Drift] Document {identifier}"
            requested = (
                f"Document `{identifier}` in `.env.example` and the configuration reference, "
                "including its purpose, default, and accepted values."
            )
        else:
            title = f"[Documentation Drift] Document CLI option {identifier}"
            requested = (
                f"Document `{identifier}` in the CLI usage reference with its behavior "
                "and examples."
            )
        if finding["kind"] == "public_api":
            title = f"[Documentation Drift] Document public API {identifier}"
            requested = (
                f"Document `{identifier}` from `{finding['source_file']}` in the API reference "
                "with its supported inputs and behavior."
            )
        reason = (
            f"{identifier} was introduced in {finding['source_file']} but is absent "
            "from current documentation."
        )
        confidence = min(0.95, 0.78 + 0.03 * len(findings))
        proposal = IssueProposal(
            title=title,
            summary=reason,
            problem=(
                f"The added `{finding['kind']}` declaration `{identifier}` is present in "
                f"`{finding['source_file']}`, but {finding['missing_from']} does not mention it."
            ),
            evidence=[
                f"Declaration: {identifier}",
                f"Kind: {finding['kind']}",
                f"Source: {finding['source_file']}",
                "Documentation checked: README.md, .env.example, and docs/*.md",
            ],
            affected_files=[finding["source_file"]],
            requested_change=requested,
            acceptance_criteria=[
                f"`{identifier}` is described accurately in the relevant documentation.",
                "Configuration defaults and allowed behavior match the implementation.",
                "No source behavior is changed by the documentation-only remediation.",
            ],
            validation_commands=["pytest -q", "git diff --check"],
            labels=["agent-recommendation"],
            source_agent=self.name,
            confidence=confidence,
            deduplication_key=f"{finding['kind']}:{identifier}",
            assign_to_copilot=True,
            finding_type=f"undocumented:{finding['kind']}:{identifier}",
        )
        return AgentDecision(
            action="report_documentation_drift",
            reason=reason,
            tool="create_issue",
            arguments={"findings": findings, "issue_proposal": proposal.model_dump(mode="json")},
            confidence=confidence,
            requires_approval=True,
        )

    def act(self, context: AgentContext, decision: AgentDecision) -> dict[str, Any]:
        return {"findings": decision.arguments.get("findings", []), "published": False}

    def validate(
        self, context: AgentContext, decision: AgentDecision, tool_result: dict[str, Any]
    ) -> dict[str, Any]:
        return {"evidence_backed": bool(tool_result.get("findings"))}
