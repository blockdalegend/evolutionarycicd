"""Thin GitHub API wrapper used by agents and the orchestrator.

All *write* operations (comments, branches, PRs, issues) are gated behind an
explicit ``dry_run`` flag. When ``AGENT_DRY_RUN`` is true (the default), the
client logs what it *would* do instead of calling the GitHub API. This makes
the whole system safe to demo without a live repository connection.

The orchestrator is additionally responsible for checking these calls against
the policy file *before* invoking them -- this client does not know about
policy itself, only about dry-run safety.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from telemetry.logger import get_logger

logger = get_logger(__name__)


def _is_dry_run() -> bool:
    return os.environ.get("AGENT_DRY_RUN", "true").lower() not in {"0", "false", "no"}


@dataclass
class PullRequestInfo:
    """A minimal snapshot of a pull request used by agents."""

    number: int
    title: str
    body: str
    changed_files: list[str]
    diff: str


class GitHubClient:
    """Wraps GitHub REST API access for repository/PR/issue operations.

    Uses PyGithub when a token is available; otherwise all read operations
    return empty/placeholder data so local demos still work without network
    access.
    """

    def __init__(self, token: str | None = None, repository: str | None = None) -> None:
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.repository = repository or os.environ.get("GITHUB_REPOSITORY", "")
        self.dry_run = _is_dry_run()
        self._gh: Any = None

    def _client(self) -> Any:
        if self._gh is None:
            from github import Github  # imported lazily; optional at runtime

            self._gh = Github(self.token) if self.token else Github()
        return self._gh

    def get_pull_request(self, number: int) -> PullRequestInfo | None:
        """Fetch PR metadata, changed files, and diff. Returns ``None`` on failure."""
        if not self.token or not self.repository:
            logger.info("No GitHub token/repository configured; returning no PR data.")
            return None
        try:
            repo = self._client().get_repo(self.repository)
            pr = repo.get_pull(number)
            changed_files = [f.filename for f in pr.get_files()]
            diff = "\n".join(f.patch or "" for f in pr.get_files())
            return PullRequestInfo(
                number=pr.number,
                title=pr.title,
                body=pr.body or "",
                changed_files=changed_files,
                diff=diff,
            )
        except Exception as exc:  # pragma: no cover - network/library failure path
            logger.warning("Failed to fetch PR #%s: %s", number, exc)
            return None

    def post_pr_comment(self, number: int, body: str) -> bool:
        """Post a comment on a pull request, honoring dry-run mode."""
        if self.dry_run:
            logger.info(
                "[dry-run] would post PR comment",
                extra={"extra_fields": {"pull_request": number, "body": body}},
            )
            return True
        if not self.token or not self.repository:
            logger.warning("Cannot post PR comment: missing token/repository.")
            return False
        try:
            repo = self._client().get_repo(self.repository)
            pr = repo.get_pull(number)
            pr.create_issue_comment(body)
            return True
        except Exception as exc:  # pragma: no cover - network/library failure path
            logger.warning("Failed to post PR comment: %s", exc)
            return False

    def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> bool:
        """Create a GitHub issue, honoring dry-run mode."""
        if self.dry_run:
            logger.info(
                "[dry-run] would create issue",
                extra={"extra_fields": {"title": title, "labels": labels or []}},
            )
            return True
        if not self.token or not self.repository:
            logger.warning("Cannot create issue: missing token/repository.")
            return False
        try:
            repo = self._client().get_repo(self.repository)
            repo.create_issue(title=title, body=body, labels=labels or [])
            return True
        except Exception as exc:  # pragma: no cover - network/library failure path
            logger.warning("Failed to create issue: %s", exc)
            return False

    def create_branch(self, branch_name: str, from_branch: str = "main") -> bool:
        """Create a branch, honoring dry-run mode."""
        if self.dry_run:
            logger.info(
                "[dry-run] would create branch",
                extra={"extra_fields": {"branch": branch_name, "from_branch": from_branch}},
            )
            return True
        if not self.token or not self.repository:
            logger.warning("Cannot create branch: missing token/repository.")
            return False
        try:
            repo = self._client().get_repo(self.repository)
            base = repo.get_branch(from_branch)
            repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base.commit.sha)
            return True
        except Exception as exc:  # pragma: no cover - network/library failure path
            logger.warning("Failed to create branch: %s", exc)
            return False
