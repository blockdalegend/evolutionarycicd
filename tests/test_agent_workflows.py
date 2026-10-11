from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
NEW_WORKFLOWS = {
    "agent-flaky-test.yml",
    "agent-build-performance.yml",
    "agent-test-impact.yml",
    "agent-documentation-drift.yml",
    "agent-effectiveness.yml",
}
ACTION_SHA = re.compile(r"^[^@]+@[a-f0-9]{40}(?:\s+#.*)?$")


def _workflows() -> dict[str, dict[str, Any]]:
    return {
        name: yaml.load((WORKFLOW_DIR / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        for name in NEW_WORKFLOWS
    }


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return job.get("steps", [])


def test_new_agent_workflows_have_expected_triggers_and_pinned_actions() -> None:
    workflows = _workflows()

    assert set(workflows) == NEW_WORKFLOWS
    for name, workflow in workflows.items():
        triggers = workflow["on"]
        assert "workflow_dispatch" in triggers
        all_steps = [step for job in workflow["jobs"].values() for step in _steps(job)]
        actions = [step["uses"] for step in all_steps if "uses" in step]
        assert actions, name
        assert all(ACTION_SHA.fullmatch(action) for action in actions), (name, actions)
        content = (WORKFLOW_DIR / name).read_text(encoding="utf-8").lower()
        assert not re.search(r"gh\s+pr\s+merge|terraform\s+apply|az\s+deployment", content)


def test_history_workflows_use_trusted_code_and_read_only_artifact_collection() -> None:
    workflows = _workflows()
    for name in ("agent-flaky-test.yml", "agent-build-performance.yml"):
        workflow = workflows[name]
        triggers = workflow["on"]
        assert triggers["workflow_run"]["workflows"] == ["CI"]
        assert "completed" in triggers["workflow_run"]["types"]
        assert "schedule" in triggers
        job = workflow["jobs"]["analyze-history"]
        assert job["permissions"]["actions"] == "read"
        checkout = next(step for step in _steps(job) if step.get("name", "").startswith("Checkout"))
        assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
        assert all("download-artifact" not in step.get("uses", "") for step in _steps(job))
        collector = next(
            step for step in _steps(job) if "collect_pipeline_context.py" in step.get("run", "")
        )
        assert collector["env"]["AGENT_HISTORY_MODE"] == (
            "tests" if name == "agent-flaky-test.yml" else "build"
        )


def test_pull_request_analysis_jobs_are_read_only_and_force_dry_run() -> None:
    workflows = _workflows()
    for name in ("agent-test-impact.yml", "agent-documentation-drift.yml"):
        workflow = workflows[name]
        assert "pull_request" in workflow["on"]
        advisory = workflow["jobs"]["advisory"]
        assert all(value == "read" for value in advisory["permissions"].values())
        run_step = next(step for step in _steps(advisory) if "--demo" in step.get("run", ""))
        assert run_step["env"]["AGENT_DRY_RUN"] == "true"
        checkout = next(
            step for step in _steps(advisory) if step.get("name", "").startswith("Checkout")
        )
        assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
        collector = next(
            step
            for step in _steps(advisory)
            if "collect_pipeline_context.py" in step.get("run", "")
        )
        assert collector["env"]["AGENT_HISTORY_MODE"] == "none"
        dispatch = workflow["jobs"]["trusted-dispatch"]
        assert dispatch["permissions"]["issues"] == "write"
        assert workflow["on"]["workflow_dispatch"]["inputs"]["dry_run"]["default"] == "true"


def test_effectiveness_is_weekly_and_keeps_merge_and_deploy_out_of_scope() -> None:
    workflow = _workflows()["agent-effectiveness.yml"]
    assert set(workflow["on"]) == {"workflow_dispatch", "schedule"}
    assert workflow["jobs"]["evaluate"]["permissions"]["issues"] == "write"
    assert workflow["jobs"]["evaluate"]["permissions"]["pull-requests"] == "read"
    collector = next(
        step
        for step in _steps(workflow["jobs"]["evaluate"])
        if "collect_pipeline_context.py" in step.get("run", "")
    )
    assert collector["env"]["AGENT_HISTORY_MODE"] == "outcomes"
