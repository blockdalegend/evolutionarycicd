from __future__ import annotations

from scripts.collect_pipeline_context import _collect_local_git_diff, _parse_junit


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