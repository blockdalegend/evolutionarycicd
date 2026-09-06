"""Local telemetry storage (newline-delimited JSON files under ``data/``).

This is intentionally simple: a single append-only ``.ndjson`` file per
telemetry stream. No database is required for the conference demo.
"""

from __future__ import annotations

import json
from pathlib import Path

from telemetry.models import AgentTelemetryRecord, PipelineRun

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TELEMETRY_FILE = DATA_DIR / "agent_telemetry.ndjson"
PIPELINE_HISTORY_FILE = DATA_DIR / "pipeline_history.json"


def record_telemetry(record: AgentTelemetryRecord, path: Path = TELEMETRY_FILE) -> None:
    """Append a single telemetry record to ``path`` as one JSON line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record.model_dump_json() + "\n")


def load_telemetry(path: Path = TELEMETRY_FILE) -> list[AgentTelemetryRecord]:
    """Load all telemetry records from ``path``, if it exists."""
    if not path.exists():
        return []
    records: list[AgentTelemetryRecord] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(AgentTelemetryRecord.model_validate_json(line))
    return records


def load_pipeline_history(path: Path = PIPELINE_HISTORY_FILE) -> list[PipelineRun]:
    """Load historical pipeline runs seeded by ``scripts/seed_demo_history.py``."""
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [PipelineRun.model_validate(item) for item in raw]
