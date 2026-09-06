"""Deterministic test-execution helpers.

Agents call into these functions instead of invoking arbitrary shell
commands. All arguments are constrained to a fixed, validated set of pytest
flags -- an LLM can request that generated tests be run, but it cannot
inject arbitrary commands.
"""

from __future__ import annotations

import subprocess  # nosec B404 - used only for fixed pytest invocations
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TestRunResult:
    """The outcome of running a pytest invocation."""

    passed: int
    failed: int
    exit_code: int
    stdout: str
    all_passed: bool = field(init=False)

    def __post_init__(self) -> None:
        self.all_passed = self.exit_code == 0


def run_pytest(
    target_paths: list[str],
    repo_root: Path,
    extra_args: list[str] | None = None,
) -> TestRunResult:
    """Run pytest against ``target_paths`` and parse a summary result.

    ``target_paths`` must be paths within the repository; no shell
    interpolation or arbitrary command strings are accepted.
    """
    allowed_extra = {"-q", "--cov=app", "--cov-report=term-missing"}
    cmd = [sys.executable, "-m", "pytest", *target_paths, "-q"]
    if extra_args:
        for arg in extra_args:
            if arg in allowed_extra:
                cmd.append(arg)

    result = subprocess.run(  # nosec B603 - fixed executable, validated args, shell=False
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    stdout = result.stdout + result.stderr
    passed, failed = _parse_summary(stdout)
    return TestRunResult(passed=passed, failed=failed, exit_code=result.returncode, stdout=stdout)


def _parse_summary(stdout: str) -> tuple[int, int]:
    """Best-effort parse of pytest's summary line, e.g. '3 passed, 1 failed'."""
    import re

    passed = failed = 0
    passed_match = re.search(r"(\d+) passed", stdout)
    failed_match = re.search(r"(\d+) failed", stdout)
    if passed_match:
        passed = int(passed_match.group(1))
    if failed_match:
        failed = int(failed_match.group(1))
    return passed, failed
