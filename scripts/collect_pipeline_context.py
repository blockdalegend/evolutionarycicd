"""Assemble an ``AgentContext`` from the local GitHub Actions environment.

Used by CI workflows to build the context object passed to
``scripts/run_agent.py``. Falls back to sensible empty defaults when running
outside of GitHub Actions (e.g. locally), so the demo always works.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os  # noqa: E402

from agents.orchestrator.orchestrator import load_github_event_context  # noqa: E402
from telemetry.store import load_pipeline_history  # noqa: E402


def collect_context() -> dict[str, object]:
    """Collect available pipeline context from environment and event payload."""
    event = load_github_event_context()
    pull_request = event.get("pull_request", {})
    history = load_pipeline_history()

    return {
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "pull_request_number": pull_request.get("number"),
        "commit_sha": pull_request.get("head", {}).get("sha"),
        "changed_files": [],
        "diff": "",
        "test_results": {},
        "coverage": {},
        "security_findings": {},
        "pipeline_history": [json.loads(run.model_dump_json()) for run in history],
    }


def main() -> None:
    """Print the collected context as JSON (used by workflow steps)."""
    print(json.dumps(collect_context(), indent=2))


if __name__ == "__main__":
    main()
