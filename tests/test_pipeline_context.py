from __future__ import annotations

from scripts.collect_pipeline_context import _parse_junit


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