"""Local git helpers used by the Merge Conflict Agent.

Deliberately narrow in scope: detect conflict markers and expose surrounding
context. No agent-authored shell commands are executed here beyond a small,
fixed allowlist of read-only ``git`` invocations.
"""

from __future__ import annotations

import subprocess  # nosec B404 - used only for fixed, read-only git invocations
from dataclasses import dataclass
from pathlib import Path

CONFLICT_START = "<<<<<<<"
CONFLICT_MID = "======="
CONFLICT_END = ">>>>>>>"


@dataclass
class ConflictBlock:
    """A single conflicted region within a file."""

    ours: str
    theirs: str
    start_line: int
    end_line: int


def find_conflicted_files(repo_root: Path) -> list[str]:
    """Return paths (relative to ``repo_root``) reported by git as conflicted."""
    result = subprocess.run(  # nosec B603 B607 - fixed read-only git command; PATH lookup is intentional
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def extract_conflict_blocks(file_path: Path) -> list[ConflictBlock]:
    """Parse conflict markers out of ``file_path`` into structured blocks."""
    lines = file_path.read_text(encoding="utf-8").splitlines()
    blocks: list[ConflictBlock] = []

    ours: list[str] = []
    theirs: list[str] = []
    state = "outside"
    start_line = 0

    for idx, line in enumerate(lines):
        if line.startswith(CONFLICT_START):
            state = "ours"
            ours, theirs = [], []
            start_line = idx
            continue
        if line.startswith(CONFLICT_MID) and state == "ours":
            state = "theirs"
            continue
        if line.startswith(CONFLICT_END) and state == "theirs":
            blocks.append(
                ConflictBlock(
                    ours="\n".join(ours),
                    theirs="\n".join(theirs),
                    start_line=start_line,
                    end_line=idx,
                )
            )
            state = "outside"
            continue
        if state == "ours":
            ours.append(line)
        elif state == "theirs":
            theirs.append(line)

    return blocks
