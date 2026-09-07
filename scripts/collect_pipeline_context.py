"""Assemble an ``AgentContext`` from the local GitHub Actions environment.

Used by CI workflows to build the context object passed to
``scripts/run_agent.py``. Falls back to sensible empty defaults when running
outside of GitHub Actions (e.g. locally), so the demo always works.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from defusedxml import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.orchestrator.orchestrator import load_github_event_context  # noqa: E402
from telemetry.store import load_pipeline_history  # noqa: E402
from tools.github.client import GitHubClient  # noqa: E402


def _read_json(path: Path) -> object:
    """Read a JSON artifact, returning an empty value when it is unavailable."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _find_artifact(name: str) -> Path:
    """Resolve a CI artifact from the configured directory or common temp paths."""
    artifact_dir = Path(os.environ.get("AGENT_ARTIFACT_DIR", tempfile.gettempdir()))
    temp_dir = Path(tempfile.gettempdir())
    for candidate in (artifact_dir / name, Path.cwd() / name, temp_dir / name):
        if candidate.exists():
            return candidate
    return artifact_dir / name


def _collect_test_sources(
    root: Path, max_files: int = 40, max_bytes: int = 20000
) -> dict[str, str]:
    """Collect bounded test source excerpts for semantic quality review."""
    sources: dict[str, str] = {}
    for path in sorted(root.glob("tests/**/*.py"))[:max_files]:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        sources[str(path.relative_to(root))] = content[:max_bytes]
    return sources


def _parse_junit(path: Path) -> dict[str, object]:
    """Extract authoritative failure details and counts from a JUnit report."""
    if not path.exists():
        return {}
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return {}
    failures: list[str] = []
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            detail = case.find("failure")
            if detail is None:
                detail = case.find("error")
            message = (detail.text or "") if detail is not None else ""
            failures.append(f"{case.get('classname', '')}.{case.get('name', '')}: {message}")
    return {
        "failures": failures,
        "tests": int(root.get("tests", 0)),
        "failures_count": int(root.get("failures", 0)) + int(root.get("errors", 0)),
    }


def _parse_coverage(path: Path) -> dict[str, object]:
    """Extract coverage percentage from coverage.py XML output."""
    if not path.exists():
        return {}
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return {}
    line_rate = root.get("line-rate")
    if line_rate is None:
        return {}
    return {"after": round(float(line_rate) * 100, 2)}


def _pull_request_number(event: dict[str, object]) -> int | None:
    """Find a PR number in pull_request and workflow_run event payloads."""
    pull_request = event.get("pull_request")
    if isinstance(pull_request, dict) and isinstance(pull_request.get("number"), int):
        return pull_request["number"]
    pull_requests = event.get("pull_requests")
    if isinstance(pull_requests, list) and pull_requests:
        first = pull_requests[0]
        if isinstance(first, dict) and isinstance(first.get("number"), int):
            return first["number"]
    configured = os.environ.get("AGENT_PULL_REQUEST_NUMBER")
    return int(configured) if configured and configured.isdigit() else None


def collect_context() -> dict[str, object]:
    """Collect available pipeline context from environment and event payload."""
    event = load_github_event_context()
    pull_request = event.get("pull_request", {})
    pull_request = pull_request if isinstance(pull_request, dict) else {}
    workflow_run = event.get("workflow_run", {})
    workflow_run = workflow_run if isinstance(workflow_run, dict) else {}
    pull_request_number = _pull_request_number(event)
    pull_request_info = (
        GitHubClient().get_pull_request(pull_request_number) if pull_request_number else None
    )
    junit = _parse_junit(_find_artifact("junit.xml"))
    coverage = _parse_coverage(_find_artifact("coverage.xml"))
    bandit = _read_json(_find_artifact("bandit.json"))
    pip_audit = _read_json(_find_artifact("pip-audit.json"))
    zizmor = _read_json(_find_artifact("zizmor.json"))
    security_findings = {
        "bandit": bandit.get("results", []) if isinstance(bandit, dict) else [],
        "pip_audit": (
            [
                dependency
                for dependency in pip_audit.get("dependencies", [])
                if dependency.get("vulns")
            ]
            if isinstance(pip_audit, dict)
            else pip_audit
        ),
        "github_actions": zizmor if isinstance(zizmor, list) else [],
        "github_actions_scan_status": (
            "completed" if isinstance(zizmor, list) else "unavailable"
        ),
    }
    history = load_pipeline_history()

    return {
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "pull_request_number": (
            pull_request_info.number if pull_request_info else pull_request_number
        ),
        "commit_sha": (
            pull_request.get("head", {}).get("sha")
            if isinstance(pull_request.get("head"), dict)
            else workflow_run.get("head_sha", os.environ.get("GITHUB_SHA", ""))
        ),
        "changed_files": pull_request_info.changed_files if pull_request_info else [],
        "diff": pull_request_info.diff if pull_request_info else "",
        "test_results": junit,
        "test_sources": _collect_test_sources(Path.cwd()),
        "coverage": coverage,
        "security_findings": security_findings,
        "pipeline_history": [json.loads(run.model_dump_json()) for run in history],
    }


def main() -> None:
    """Print the collected context as JSON (used by workflow steps)."""
    print(json.dumps(collect_context(), indent=2))


if __name__ == "__main__":
    main()
