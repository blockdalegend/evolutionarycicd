"""Wrappers around deterministic security scanners.

The Supply Chain Agent consumes the output of these tools; it never pretends
to be a scanner itself. If a tool is unavailable, the wrapper returns an
empty, clearly-labeled result instead of fabricating findings.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 - used only for fixed, read-only scanner invocations
import sys
from dataclasses import dataclass, field
from pathlib import Path
from shutil import which


@dataclass
class ScanResult:
    """Normalized result of running a security scanner."""

    tool: str
    ran_successfully: bool
    findings: list[dict[str, object]] = field(default_factory=list)
    raw_output: str = ""


def run_bandit(target_paths: list[str], repo_root: Path) -> ScanResult:
    """Run Bandit against ``target_paths`` and return normalized findings."""
    cmd = [sys.executable, "-m", "bandit", "-r", *target_paths, "-f", "json"]
    result = subprocess.run(  # nosec B603 - fixed executable, validated args, shell=False
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return ScanResult(tool="bandit", ran_successfully=False, raw_output=result.stdout)
    findings = data.get("results", [])
    return ScanResult(
        tool="bandit", ran_successfully=True, findings=findings, raw_output=result.stdout
    )


def run_pip_audit(requirements_path: Path) -> ScanResult:
    """Run pip-audit against a requirements file and return normalized findings."""
    if not requirements_path.exists():
        return ScanResult(
            tool="pip-audit",
            ran_successfully=False,
            raw_output="requirements file not found",
        )
    cmd = [
        sys.executable,
        "-m",
        "pip_audit",
        "-r",
        str(requirements_path),
        "-f",
        "json",
    ]
    result = subprocess.run(  # nosec B603 - fixed executable, validated args, shell=False
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return ScanResult(tool="pip-audit", ran_successfully=False, raw_output=result.stdout)
    dependencies = data.get("dependencies", data) if isinstance(data, dict) else data
    findings = (
        [dep for dep in dependencies if dep.get("vulns")]
        if isinstance(dependencies, list)
        else []
    )
    return ScanResult(
        tool="pip-audit", ran_successfully=True, findings=findings, raw_output=result.stdout
    )


def run_zizmor(workflows_dir: Path) -> ScanResult:
    """Audit GitHub Actions workflows for security vulnerabilities."""
    executable = which("zizmor")
    if executable is None:
        return ScanResult(tool="zizmor", ran_successfully=False, raw_output="zizmor not found")
    cmd = [executable, "--format", "json", str(workflows_dir)]
    result = subprocess.run(  # nosec B603 - fixed scanner executable, shell=False
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return ScanResult(tool="zizmor", ran_successfully=False, raw_output=result.stdout)
    findings = data if isinstance(data, list) else data.get("findings", [])
    return ScanResult(
        tool="zizmor",
        ran_successfully=True,
        findings=findings if isinstance(findings, list) else [],
        raw_output=result.stdout,
    )


def find_unpinned_requirements(requirements_path: Path) -> list[str]:
    """Return requirement lines that do not pin an exact version (``==``)."""
    if not requirements_path.exists():
        return []
    unpinned = []
    for line in requirements_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "==" not in stripped:
            unpinned.append(stripped)
    return unpinned


def find_unpinned_actions(workflows_dir: Path) -> list[str]:
    """Return ``uses:`` references in workflows that are not pinned to a full SHA."""
    unpinned: list[str] = []
    if not workflows_dir.exists():
        return unpinned
    for workflow_file in sorted(workflows_dir.glob("*.yml")):
        for line in workflow_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("uses:"):
                ref = stripped.split("uses:", 1)[1].strip()
                # A pinned action reference looks like owner/repo@<40-char sha>
                if "@" not in ref:
                    unpinned.append(f"{workflow_file.name}: {ref}")
                    continue
                _, _, tag = ref.partition("@")
                if len(tag) != 40 or not all(c in "0123456789abcdef" for c in tag.lower()):
                    unpinned.append(f"{workflow_file.name}: {ref}")
    return unpinned
