"""Extract compact, location-aware evidence from Python source files."""

from __future__ import annotations

import ast
from typing import Any


def _source(node: ast.AST, source: str) -> str:
    """Return the original expression where Python exposes its source span."""
    return ast.get_source_segment(source, node) or ast.unparse(node)


def _location(node: ast.AST) -> dict[str, int]:
    """Return a one-based source location for an AST node."""
    return {
        "line": getattr(node, "lineno", 0),
        "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 0)),
    }


class _EvidenceVisitor(ast.NodeVisitor):
    def __init__(self, source: str, is_test_file: bool) -> None:
        self.source = source
        self.is_test_file = is_test_file
        self.functions: list[dict[str, Any]] = []
        self._function_stack: list[dict[str, Any]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        function: dict[str, Any] = {
            "name": node.name,
            "line": node.lineno,
            "end_line": getattr(node, "end_lineno", node.lineno),
            "arguments": [
                argument.arg
                for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            ],
            "decorators": [_source(decorator, self.source) for decorator in node.decorator_list],
            "conditions": [],
            "assertions": [],
            "exceptions": [],
            "mocks": [],
            "fixtures": [],
            "calls": [],
        }
        self.functions.append(function)
        self._function_stack.append(function)
        for argument in node.args.args + node.args.kwonlyargs:
            if argument.arg in {"monkeypatch", "mocker", "request"}:
                function["fixtures"].append(argument.arg)
        if any("parametrize" in decorator for decorator in function["decorators"]):
            function["fixtures"].append("pytest.mark.parametrize")
        for child in node.body:
            self.visit(child)
        self._function_stack.pop()

    @property
    def current(self) -> dict[str, Any] | None:
        return self._function_stack[-1] if self._function_stack else None

    def visit_If(self, node: ast.If) -> None:
        if self.current is not None:
            self.current["conditions"].append(
                {"expression": _source(node.test, self.source), **_location(node.test)}
            )
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        if self.current is not None:
            self.current["conditions"].append(
                {"expression": _source(node.test, self.source), **_location(node.test)}
            )
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        if self.current is not None:
            assertion = {"expression": _source(node.test, self.source), **_location(node)}
            if node.msg is not None:
                assertion["message"] = _source(node.msg, self.source)
            self.current["assertions"].append(assertion)
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        if self.current is not None:
            self.current["exceptions"].append(
                {
                    "expression": _source(node.exc, self.source) if node.exc else "re-raise",
                    **_location(node),
                }
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self.current is not None:
            call = _source(node, self.source)
            self.current["calls"].append({"expression": call, **_location(node)})
            if (
                isinstance(node.func, ast.Name)
                and node.func.id in {"patch", "spy", "stub"}
            ) or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"patch", "spy", "stub"}
            ) or "mock.patch" in call:
                self.current["mocks"].append({"expression": call, **_location(node)})
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "raises",
                "warns",
            }:
                self.current["exceptions"].append(
                    {"expression": call, "kind": "pytest_context", **_location(node)}
                )
        self.generic_visit(node)


def extract_python_evidence(path: str, source: str) -> dict[str, Any]:
    """Return deterministic function and test evidence for one Python file."""
    tree = ast.parse(source, filename=path)
    is_test_file = path.startswith("tests/") or path.startswith("test_")
    visitor = _EvidenceVisitor(source, is_test_file)
    visitor.visit(tree)
    tests = [
        function
        for function in visitor.functions
        if function["name"].startswith("test_") or is_test_file
    ]
    return {
        "file": path,
        "is_test_file": is_test_file,
        "functions": visitor.functions,
        "tests": tests,
    }