"""Generate ~30 seed demo pipeline history records under ``data/``.

This gives the Pipeline Optimizer Agent something realistic to analyze
during the conference demo: a flaky test, a repeatedly failing stage, a slow
test suite, and a recurring dependency failure, mixed in with healthy runs.
"""

from __future__ import annotations

import json
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telemetry.models import PipelineRun  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUTPUT_PATH = DATA_DIR / "pipeline_history.json"

WORKFLOWS = ["ci.yml", "agent-test-quality.yml"]


def _build_runs(seed: int = 42) -> list[PipelineRun]:
    rng = random.Random(seed)  # nosec B311 - non-cryptographic demo data generation
    runs: list[PipelineRun] = []
    start = datetime(2024, 1, 1, tzinfo=UTC)

    for i in range(30):
        run_id = f"run-{1000 + i}"
        workflow = WORKFLOWS[0] if i % 4 != 0 else WORKFLOWS[1]
        timestamp = start + timedelta(hours=i * 6)

        # Introduce a recurring flaky test roughly every 5th CI run.
        if workflow == "ci.yml" and i % 5 == 0:
            runs.append(
                PipelineRun(
                    run_id=run_id,
                    workflow=workflow,
                    status="failure",
                    duration_seconds=rng.uniform(180, 220),
                    failed_stage="pytest",
                    failure_reason="flaky:test_payment_gateway_timeout",
                    retry_count=1,
                    agent_actions=["failure_analysis_agent:reported"],
                    timestamp=timestamp,
                )
            )
            continue

        # Introduce a repeatedly failing lint stage roughly every 7th run.
        if workflow == "ci.yml" and i % 7 == 0:
            runs.append(
                PipelineRun(
                    run_id=run_id,
                    workflow=workflow,
                    status="failure",
                    duration_seconds=rng.uniform(60, 90),
                    failed_stage="ruff",
                    failure_reason="lint_error",
                    retry_count=0,
                    agent_actions=[],
                    timestamp=timestamp,
                )
            )
            continue

        # Introduce a recurring dependency failure roughly every 9th run.
        if i % 9 == 0:
            runs.append(
                PipelineRun(
                    run_id=run_id,
                    workflow=workflow,
                    status="failure",
                    duration_seconds=rng.uniform(30, 50),
                    failed_stage="dependency_install",
                    failure_reason="dependency:requests_registry_timeout",
                    retry_count=2,
                    agent_actions=["supply_chain_agent:reported"],
                    timestamp=timestamp,
                )
            )
            continue

        # Slow-but-successful runs roughly every 6th run (simulating a slow
        # test suite that occasionally spikes).
        duration = rng.uniform(500, 650) if i % 6 == 0 else rng.uniform(120, 200)

        runs.append(
            PipelineRun(
                run_id=run_id,
                workflow=workflow,
                status="success",
                duration_seconds=duration,
                failed_stage=None,
                failure_reason=None,
                retry_count=0,
                agent_actions=["test_quality_agent:no_action"],
                timestamp=timestamp,
            )
        )

    return runs


def main() -> None:
    """Write the seeded pipeline history to ``data/pipeline_history.json``."""
    runs = _build_runs()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps([json.loads(r.model_dump_json()) for r in runs], indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(runs)} seed pipeline runs to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
