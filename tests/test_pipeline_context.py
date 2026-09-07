from __future__ import annotations

from scripts.collect_pipeline_context import (
    _collect_local_git_diff,
    _collect_static_evidence,
    _parse_coverage,
    _parse_junit,
)


def test_parse_junit_aggregates_nested_pytest_suite(tmp_path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        """
        <testsuites>
          <testsuite name="pytest" tests="27" failures="0" errors="0">
            <testcase classname="tests.test_example" name="test_passes" />
          </testsuite>
        </testsuites>
        """,
        encoding="utf-8",
    )

    assert _parse_junit(report) == {
        "failures": [],
        "tests": 27,
        "failures_count": 0,
    }


def test_parse_coverage_returns_missing_lines_and_branches(tmp_path) -> None:
        report = tmp_path / "coverage.xml"
        report.write_text(
                """
                <coverage line-rate="0.75">
                    <packages><package name="app"><classes>
                        <class filename="app/service.py" line-rate="0.75">
                        <lines>
                            <line number="10" hits="1" branch="true"
                                        condition-coverage="50% (1/2)" />
                            <line number="11" hits="0" />
                        </lines>
                        </class>
                    </classes></package></packages>
                </coverage>
                """,
                encoding="utf-8",
        )

        assert _parse_coverage(report) == {
                "after": 75.0,
                "branch_coverage_available": True,
                "files": [
                        {
                                "file": "app/service.py",
                                "line_rate": 75.0,
                                "missing_lines": [11],
                                "branches": [{"line": 10, "covered": 1, "total": 2}],
                        }
                ],
        }


def test_collect_local_git_diff_returns_branch_evidence(monkeypatch) -> None:
    outputs = iter(
        [
            type("Result", (), {"stdout": "app/main.py\ntests/test_main.py\n"})(),
            type("Result", (), {"stdout": "diff --git a/app/main.py b/app/main.py"})(),
        ]
    )
    monkeypatch.setattr(
        "scripts.collect_pipeline_context.subprocess.run",
        lambda *args, **kwargs: next(outputs),
    )

    changed_files, diff = _collect_local_git_diff()

    assert changed_files == ["app/main.py", "tests/test_main.py"]
    assert diff.startswith("diff --git")


def test_collect_static_evidence_includes_changed_functions_and_tests(tmp_path) -> None:
    production = tmp_path / "app" / "service.py"
    production.parent.mkdir()
    production.write_text(
        "def charge(amount):\n    if amount <= 0:\n        return False\n    return True\n",
        encoding="utf-8",
    )
    test_sources = {
        "tests/test_service.py": "def test_zero():\n    assert charge(0) is False\n"
    }

    evidence = _collect_static_evidence(
        tmp_path, ["app/service.py", "tests/test_service.py"], test_sources
    )

    assert evidence["parse_errors"] == []
    files = {item["file"]: item for item in evidence["files"]}
    assert files["app/service.py"]["functions"][0]["name"] == "charge"
    assert files["app/service.py"]["functions"][0]["conditions"][0]["expression"] == (
        "amount <= 0"
    )
    assert files["tests/test_service.py"]["tests"][0]["assertions"][0]["expression"] == (
        "charge(0) is False"
    )