"""Assemble an ``AgentContext`` from the local GitHub Actions environment.

Used by CI workflows to build the context object passed to
``scripts/run_agent.py``. Falls back to sensible empty defaults when running
outside of GitHub Actions (e.g. locally), so the demo always works.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import subprocess  # nosec B404 - used only for fixed, read-only git invocations
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.orchestrator.orchestrator import load_github_event_context  # noqa: E402
from telemetry.models import KNOWN_AGENT_NAMES  # noqa: E402
from telemetry.store import load_pipeline_history  # noqa: E402
from tools.analysis.ast_extractor import extract_python_evidence  # noqa: E402
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
    except (OSError, ET.ParseError, DefusedXmlException):
        return {}
    suites = list(root.iter("testsuite"))
    failures: list[str] = []
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            detail = case.find("failure")
            if detail is None:
                detail = case.find("error")
            message = (detail.text or "") if detail is not None else ""
            failures.append(f"{case.get('classname', '')}.{case.get('name', '')}: {message}")

    def _suite_count(attribute: str) -> int:
        total = 0
        for suite in suites:
            try:
                total += int(suite.get(attribute, 0) or 0)
            except ValueError:
                continue
        return total

    return {
        "failures": failures,
        "tests": _suite_count("tests") or int(root.get("tests", 0)),
        "failures_count": _suite_count("failures") + _suite_count("errors"),
    }


def _parse_junit_history(content: bytes, run_id: str) -> dict[str, object]:
    """Parse bounded JUnit XML into stable per-test historical outcomes."""
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, DefusedXmlException):
        return {}
    cases: list[dict[str, object]] = []
    failed_test_ids: set[str] = set()
    for case in root.iter("testcase"):
        name = case.get("name", "").split("[", 1)[0]
        if not name:
            continue
        classname = case.get("classname", "")
        test_id = f"{classname}::{name}" if classname else name
        test_id = re.sub(r"[^A-Za-z0-9_.:/-]", "_", test_id).strip("_")[:300]
        outcome = (
            "failed"
            if case.find("failure") is not None or case.find("error") is not None
            else "skipped"
            if case.find("skipped") is not None
            else "passed"
        )
        failure_node = case.find("failure")
        if failure_node is None:
            failure_node = case.find("error")
        detail = ""
        if failure_node is not None:
            detail = " ".join(
                part for part in (failure_node.get("message", ""), failure_node.text or "") if part
            )[:1200]
        try:
            duration = float(case.get("time", 0) or 0)
        except ValueError:
            duration = 0.0
        if not math.isfinite(duration) or duration < 0:
            duration = 0.0
        successful_rerun = outcome == "passed" and test_id in failed_test_ids
        if outcome == "failed":
            failed_test_ids.add(test_id)
        cases.append(
            {
                "test_id": test_id,
                "outcome": outcome,
                "duration_seconds": duration,
                "failure_detail": detail,
                "successful_rerun": successful_rerun,
            }
        )
    return {"run_id": run_id, "cases": cases}


def _iso_duration(start: object, end: object) -> float | None:
    """Calculate elapsed seconds only when both GitHub timestamps are present."""
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        started = datetime.fromisoformat(start.replace("Z", "+00:00"))
        finished = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=UTC)
        return max((finished - started).total_seconds(), 0.0)
    except ValueError:
        return None


def _collect_github_history(
    client: GitHubClient,
    mode: str = "all",
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, Any]],
]:
    """Collect trusted CI timing, JUnit, and telemetry artifacts through GitHub APIs."""
    test_history: list[dict[str, object]] = []
    build_history: list[dict[str, object]] = []
    telemetry_history: list[dict[str, object]] = []
    if mode == "none":
        return test_history, build_history, telemetry_history, []
    runs = client.list_workflow_runs(per_page=50)
    ci_runs = (
        client.list_workflow_runs(per_page=50, workflow="ci.yml")
        if mode in {"all", "build", "tests", "outcomes"}
        else []
    )
    runs_by_id = {run.get("id"): run for run in runs if isinstance(run.get("id"), int)}
    runs_by_id.update({run.get("id"): run for run in ci_runs if isinstance(run.get("id"), int)})
    runs = list(runs_by_id.values())
    for run in runs:
        run_id = run.get("id")
        if not isinstance(run_id, int):
            continue
        if run.get("status") != "completed":
            continue
        run_key = str(run_id)
        workflow_path = str(run.get("path", "")).split("@", 1)[0]
        is_ci_run = workflow_path.endswith("/ci.yml")
        if mode in {"build", "tests"} and not is_ci_run:
            continue
        needs_artifacts = mode in {"all", "outcomes"} or (is_ci_run and mode in {"build", "tests"})
        artifacts = client.list_run_artifacts(run_id) if needs_artifacts else []
        if is_ci_run and mode in {"all", "build"}:
            run_duration = _iso_duration(
                run.get("run_started_at") or run.get("created_at"), run.get("updated_at")
            )
            jobs = client.get_workflow_jobs(run_id)
            job_data: list[dict[str, object]] = []
            for job in jobs:
                duration = _iso_duration(job.get("started_at"), job.get("completed_at"))
                if duration is not None:
                    job_data.append(
                        {
                            "name": job.get("name", "unknown"),
                            "duration_seconds": duration,
                            "conclusion": job.get("conclusion", "unknown"),
                        }
                    )
            if run_duration is not None:
                build_history.append(
                    {
                        "run_id": run_key,
                        "workflow": run.get("name", "CI"),
                        "workflow_path": workflow_path,
                        "status": run.get("conclusion", "unknown"),
                        "duration_seconds": run_duration,
                        "jobs": job_data,
                        "artifact_bytes": sum(
                            int(artifact.get("size_in_bytes", 0) or 0) for artifact in artifacts
                        ),
                        "timestamp": run.get("run_started_at", ""),
                    }
                )
        for artifact in artifacts:
            name = artifact.get("name")
            artifact_id = artifact.get("id")
            if artifact.get("expired") or not isinstance(artifact_id, int):
                continue
            collect_telemetry = mode in {"all", "outcomes"} and name == "agent-telemetry"
            collect_tests = mode in {"all", "tests"} and is_ci_run and name == "test-results"
            if not collect_telemetry and not collect_tests:
                continue
            archive = client.download_run_artifact(artifact_id)
            if archive is None:
                continue
            try:
                with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                    members = [item for item in bundle.infolist() if not item.is_dir()]
                    if len(members) > 20:
                        continue
                    if collect_tests:
                        report = next(
                            (
                                item
                                for item in members
                                if Path(item.filename).name == "junit.xml"
                                and item.file_size <= 5_000_000
                            ),
                            None,
                        )
                        if report is not None:
                            parsed = _parse_junit_history(bundle.read(report), run_key)
                            if parsed:
                                test_history.append(parsed)
                    else:
                        for item in members:
                            if (
                                Path(item.filename).name != "agent_telemetry.ndjson"
                                or item.file_size > 5_000_000
                            ):
                                continue
                            for line in bundle.read(item).decode("utf-8").splitlines():
                                if len(telemetry_history) >= 1000:
                                    break
                                try:
                                    row = json.loads(line)
                                except json.JSONDecodeError:
                                    continue
                                if (
                                    isinstance(row, dict)
                                    and row.get("agent") in KNOWN_AGENT_NAMES
                                ):
                                    telemetry_history.append(row)
            except (OSError, zipfile.BadZipFile, UnicodeDecodeError, RuntimeError):
                continue
    lifecycle = client.list_agent_issue_lifecycle(runs) if mode in {"all", "outcomes"} else []
    live_issue_numbers = {
        event.get("issue_number") for event in lifecycle if event.get("event") == "issue_created"
    }
    live_pr_links = {
        (event.get("issue_number"), event.get("event"), event.get("associated_pull_request"))
        for event in lifecycle
        if event.get("event") in {"issue_linked_pr", "pr_merged", "pr_validation_result"}
    }
    telemetry_history = [
        event
        for event in telemetry_history
        if not (
            event.get("event") == "issue_created"
            and event.get("issue_number") in live_issue_numbers
        )
        and not (
            event.get("event") in {"issue_linked_pr", "pr_merged", "pr_validation_result"}
            and (
                event.get("issue_number"),
                event.get("event"),
                event.get("associated_pull_request"),
            )
            in live_pr_links
        )
    ]
    telemetry_history.extend(lifecycle)
    return test_history[:50], build_history[:50], telemetry_history[-2000:], runs


def _parse_coverage(path: Path) -> dict[str, object]:
    """Extract aggregate and per-file line/branch facts from coverage XML."""
    if not path.exists():
        return {}
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return {}
    line_rate = root.get("line-rate")
    if line_rate is None:
        return {}
    files: list[dict[str, object]] = []
    has_branches = False
    for class_node in root.findall(".//class"):
        filename = class_node.get("filename", "")
        line_node_list = class_node.findall("./lines/line")
        missing_lines = [
            int(line.get("number", 0))
            for line in line_node_list
            if line.get("hits") == "0" and line.get("number")
        ]
        branches: list[dict[str, int]] = []
        for line in line_node_list:
            condition_coverage = line.get("condition-coverage", "")
            match = re.search(r"\((\d+)\s*/\s*(\d+)\)", condition_coverage)
            if match is None:
                continue
            has_branches = True
            branches.append(
                {
                    "line": int(line.get("number", 0)),
                    "covered": int(match.group(1)),
                    "total": int(match.group(2)),
                }
            )
        files.append(
            {
                "file": filename,
                "line_rate": round(float(class_node.get("line-rate", 0)) * 100, 2),
                "missing_lines": missing_lines,
                "branches": branches,
            }
        )
    return {
        "after": round(float(line_rate) * 100, 2),
        "branch_coverage_available": has_branches,
        "files": files,
    }


def _collect_static_evidence(
    root: Path, changed_files: list[str], test_sources: dict[str, str]
) -> dict[str, object]:
    """Build AST evidence for changed Python files and collected tests."""
    paths = set(path for path in changed_files if path.endswith(".py"))
    paths.update(path for path in test_sources if path.endswith(".py"))
    files: list[dict[str, object]] = []
    parse_errors: list[dict[str, str]] = []
    for relative_path in sorted(paths):
        if relative_path in test_sources:
            source = test_sources[relative_path]
        else:
            path = root / relative_path
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                parse_errors.append({"file": relative_path, "error": str(exc)})
                continue
        try:
            files.append(extract_python_evidence(relative_path, source))
        except (SyntaxError, ValueError) as exc:
            parse_errors.append({"file": relative_path, "error": str(exc)})
    return {"files": files, "parse_errors": parse_errors}


def _collect_local_git_diff() -> tuple[list[str], str]:
    """Use the current branch diff as PR evidence for local agent runs."""
    commands = [
        ["git", "diff", "--name-only", "main...HEAD"],
        ["git", "diff", "main...HEAD"],
    ]
    outputs: list[str] = []
    for command in commands:
        result = subprocess.run(  # nosec B603 B607 - fixed read-only git commands
            command,
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=False,
        )
        outputs.append(result.stdout)
    changed_files = [line for line in outputs[0].splitlines() if line.strip()]
    return changed_files, outputs[1]


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
        "github_actions_scan_status": ("completed" if isinstance(zizmor, list) else "unavailable"),
    }
    history = load_pipeline_history()
    client = GitHubClient()
    history_mode = os.environ.get("AGENT_HISTORY_MODE", "all").lower()
    if history_mode not in {"all", "build", "tests", "outcomes", "none"}:
        history_mode = "none"
    test_history, build_history, telemetry_history, workflow_runs = _collect_github_history(
        client, history_mode
    )
    if not test_history and isinstance(junit, dict) and junit:
        # The current JUnit report remains usable even when prior artifacts expired.
        current_xml = _find_artifact("junit.xml")
        if current_xml.exists():
            try:
                parsed_current = _parse_junit_history(
                    current_xml.read_bytes(), os.environ.get("GITHUB_RUN_ID", "local")
                )
                if parsed_current:
                    test_history.append(parsed_current)
            except OSError:
                pass
    local_changed_files, local_diff = _collect_local_git_diff()
    changed_files = pull_request_info.changed_files if pull_request_info else local_changed_files
    diff = pull_request_info.diff if pull_request_info else local_diff
    test_sources = _collect_test_sources(Path.cwd())
    source_files: dict[str, str] = {}
    for source_path in sorted(Path.cwd().glob("app/**/*.py"))[:100]:
        if source_path.stat().st_size > 100_000:
            continue
        try:
            source_files[source_path.relative_to(Path.cwd()).as_posix()] = source_path.read_text(
                encoding="utf-8"
            )
        except (OSError, UnicodeDecodeError):
            continue
    source_ref = (
        pull_request.get("head", {}).get("sha")
        if isinstance(pull_request.get("head"), dict)
        else None
    )
    if not source_ref and workflow_run.get("pull_requests"):
        first_pull = workflow_run["pull_requests"][0]
        if isinstance(first_pull, dict):
            pull_head = first_pull.get("head")
            nested_sha = pull_head.get("sha") if isinstance(pull_head, dict) else None
            source_ref = first_pull.get("head_sha") or first_pull.get("sha") or nested_sha
    if pull_request_info and source_ref:
        for changed_path in changed_files[:100]:
            content = client.get_file_contents(changed_path, source_ref)
            if content is not None:
                source_files[changed_path] = content
    else:
        for changed_path in changed_files[:100]:
            path = Path(changed_path)
            if path.is_file() and path.stat().st_size <= 100_000:
                try:
                    source_files[changed_path] = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue

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
        "changed_files": changed_files,
        "diff": diff,
        "test_results": junit,
        "test_history": test_history,
        "build_history": build_history or [json.loads(run.model_dump_json()) for run in history],
        "telemetry": telemetry_history,
        "test_sources": test_sources,
        "source_files": source_files,
        "coverage": coverage,
        "static_evidence": _collect_static_evidence(Path.cwd(), changed_files, test_sources),
        "security_findings": security_findings,
        "pipeline_history": [json.loads(run.model_dump_json()) for run in history],
    }


def main() -> None:
    """Print the collected context as JSON (used by workflow steps)."""
    print(json.dumps(collect_context(), indent=2))


if __name__ == "__main__":
    main()
