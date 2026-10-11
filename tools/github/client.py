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

import base64
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from telemetry.logger import get_logger
from telemetry.models import KNOWN_AGENT_NAMES

logger = get_logger(__name__)

MAX_GITHUB_ISSUE_BODY_CHARS = 60000


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
        self.copilot_token = os.environ.get("COPILOT_TOKEN", self.token)
        self.repository = repository or os.environ.get("GITHUB_REPOSITORY", "")
        self.dry_run = _is_dry_run()
        self._gh: Any = None

    def _client(self) -> Any:
        if self._gh is None:
            from github import Github  # imported lazily; optional at runtime

            self._gh = Github(self.token) if self.token else Github()
        return self._gh

    def _api_get(self, path: str, params: dict[str, Any] | None = None) -> Any | None:
        """Perform a bounded authenticated read from the GitHub REST API."""
        if not self.token or not self.repository:
            return None
        url = (
            path
            if path.startswith("https://")
            else f"https://api.github.com/repos/{self.repository}/{path.lstrip('/')}"
        )
        try:
            response = requests.get(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params=params,
                timeout=30,
            )
            if response.status_code != 200:
                logger.warning("GitHub API read failed (%s): %s", response.status_code, path)
                return None
            return response
        except Exception as exc:  # pragma: no cover - network/library failure path
            logger.warning("GitHub API read failed for %s: %s", path, exc)
            return None

    def list_workflow_runs(
        self, per_page: int = 100, workflow: str | None = None
    ) -> list[dict[str, Any]]:
        """Return recent workflow-run metadata for historical analysis."""
        endpoint = (
            f"actions/workflows/{quote(workflow, safe='')}/runs" if workflow else "actions/runs"
        )
        response = self._api_get(endpoint, {"per_page": min(max(per_page, 1), 100)})
        if response is None:
            return []
        try:
            runs = response.json().get("workflow_runs", [])
            return [run for run in runs if isinstance(run, dict)]
        except (ValueError, AttributeError):
            return []

    def get_workflow_jobs(self, run_id: int) -> list[dict[str, Any]]:
        """Return job timing metadata without inferring unreported CI stages."""
        response = self._api_get(f"actions/runs/{run_id}/jobs", {"per_page": 100})
        if response is None:
            return []
        try:
            jobs = response.json().get("jobs", [])
            return [job for job in jobs if isinstance(job, dict)]
        except (ValueError, AttributeError):
            return []

    def list_run_artifacts(self, run_id: int) -> list[dict[str, Any]]:
        """Return artifact metadata for a workflow run."""
        response = self._api_get(f"actions/runs/{run_id}/artifacts", {"per_page": 100})
        if response is None:
            return []
        try:
            artifacts = response.json().get("artifacts", [])
            return [artifact for artifact in artifacts if isinstance(artifact, dict)]
        except (ValueError, AttributeError):
            return []

    def download_run_artifact(self, artifact_id: int, max_bytes: int = 20_000_000) -> bytes | None:
        """Download a bounded artifact archive; callers must validate its contents."""
        response = self._api_get(f"actions/artifacts/{artifact_id}/zip")
        if response is None:
            return None
        content = response.content
        if len(content) > max_bytes:
            logger.warning("GitHub artifact %s exceeded the download size limit", artifact_id)
            return None
        return content

    def get_file_contents(self, path: str, ref: str, max_bytes: int = 100_000) -> str | None:
        """Read bounded UTF-8 repository content at an exact commit without executing it."""
        encoded_path = quote(path, safe="/")
        response = self._api_get(f"contents/{encoded_path}", {"ref": ref})
        if response is None:
            return None
        try:
            payload = response.json()
            if payload.get("encoding") != "base64":
                return None
            if payload.get("size", 0) > max_bytes:
                return None
            content = base64.b64decode(payload.get("content", ""), validate=False)
            if len(content) > max_bytes:
                return None
            return content.decode("utf-8")
        except (ValueError, UnicodeDecodeError, AttributeError):
            return None

    def list_agent_issue_lifecycle(
        self, workflow_runs: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """Reconcile agent issue markers with linked PR and CI outcomes."""
        response = self._api_get(
            "issues", {"state": "all", "per_page": 100, "sort": "created", "direction": "desc"}
        )
        if response is None:
            return []
        try:
            issues = response.json()
        except ValueError:
            return []
        if not isinstance(issues, list):
            return []
        runs = workflow_runs or []
        outcomes: list[dict[str, Any]] = []
        for issue in issues:
            if not isinstance(issue, dict) or issue.get("pull_request"):
                continue
            body = issue.get("body") or ""
            if not isinstance(body, str):
                continue
            agent_match = re.search(r"evolutionary-cicd:source-agent=([a-z0-9_]+)", body)
            if agent_match is None or agent_match.group(1) not in KNOWN_AGENT_NAMES:
                continue
            number = issue.get("number")
            if not isinstance(number, int):
                continue
            agent = agent_match.group(1)
            fingerprint_match = re.search(r"evolutionary-cicd:fingerprint=([a-f0-9]+)", body)
            base_record = {
                "agent": agent,
                "issue_number": number,
                "issue_fingerprint": fingerprint_match.group(1) if fingerprint_match else None,
                "issue_created_at": issue.get("created_at"),
                "timestamp": issue.get("created_at"),
                "issue_closed_at": issue.get("closed_at"),
                "event": "issue_created",
            }
            outcomes.append(base_record)
            raw_labels = issue.get("labels", [])
            issue_labels = {
                label.get("name")
                for label in (raw_labels if isinstance(raw_labels, list) else [])
                if isinstance(label, dict) and isinstance(label.get("name"), str)
            }
            if "agent-remediation-verified" in issue_labels:
                outcomes.append(
                    {
                        **base_record,
                        "event": "remediation_verified",
                        "timestamp": issue.get("updated_at"),
                    }
                )
            for label, result in (
                ("agent-measured-target-met", "target_met"),
                ("agent-measured-target-missed", "target_missed"),
            ):
                if label in issue_labels:
                    outcomes.append(
                        {
                            **base_record,
                            "event": "measured_outcome",
                            "measured_outcome": result,
                            "timestamp": issue.get("updated_at"),
                        }
                    )
            timeline = self._api_get(f"issues/{number}/timeline")
            linked_prs: set[int] = set()
            merged_prs: set[int] = set()
            if timeline is not None:
                try:
                    timeline_events = timeline.json()
                except ValueError:
                    timeline_events = []
                if isinstance(timeline_events, list):
                    for timeline_event in timeline_events:
                        if (
                            not isinstance(timeline_event, dict)
                            or timeline_event.get("event") != "cross-referenced"
                        ):
                            continue
                        source = timeline_event.get("source")
                        if not isinstance(source, dict):
                            continue
                        source_issue = source.get("issue") or {}
                        if not isinstance(source_issue, dict):
                            continue
                        source_number = source_issue.get("number")
                        if isinstance(source_number, int) and source_issue.get("pull_request"):
                            linked_prs.add(source_number)
            for pull_number in sorted(linked_prs):
                pull_response = self._api_get(f"pulls/{pull_number}")
                if pull_response is None:
                    continue
                try:
                    pull = pull_response.json()
                except ValueError:
                    continue
                if not isinstance(pull, dict):
                    continue
                pr_record = {
                    **base_record,
                    "event": "issue_linked_pr",
                    "associated_pull_request": pull_number,
                    "pr_created_at": pull.get("created_at"),
                    "pr_merged_at": pull.get("merged_at"),
                    "timestamp": pull.get("created_at"),
                }
                outcomes.append(pr_record)
                if pull.get("merged") is True:
                    merged_prs.add(pull_number)
                    outcomes.append(
                        {
                            **pr_record,
                            "event": "pr_merged",
                            "timestamp": pull.get("merged_at"),
                        }
                    )
                matching_runs: list[dict[str, Any]] = []
                for run in runs:
                    pull_requests = run.get("pull_requests", []) if isinstance(run, dict) else []
                    if (
                        isinstance(run, dict)
                        and isinstance(pull_requests, list)
                        and any(
                            isinstance(pr, dict) and pr.get("number") == pull_number
                            for pr in pull_requests
                        )
                        and run.get("status") == "completed"
                    ):
                        matching_runs.append(run)
                if matching_runs:
                    latest = max(matching_runs, key=lambda run: str(run.get("updated_at", "")))
                    outcomes.append(
                        {
                            **pr_record,
                            "event": "pr_validation_result",
                            "pr_validation_result": latest.get("conclusion", "unknown"),
                            "timestamp": latest.get("updated_at"),
                        }
                    )
            if issue.get("state") == "closed" and not merged_prs:
                outcomes.append(
                    {
                        **base_record,
                        "event": "issue_rejected",
                        "timestamp": issue.get("closed_at"),
                    }
                )
        return outcomes

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
        return self.create_issue_record(title, body, labels)["created"]

    def create_issue_record(
        self, title: str, body: str, labels: list[str] | None = None
    ) -> dict[str, Any]:
        """Create an issue and return its number and URL when available."""
        if len(body) > MAX_GITHUB_ISSUE_BODY_CHARS:
            body = (
                body[:MAX_GITHUB_ISSUE_BODY_CHARS]
                + "\n\n[Issue body truncated to fit GitHub's size limit.]"
            )
        if self.dry_run:
            logger.info(
                "WOULD CREATE ISSUE",
                extra={"extra_fields": {"title": title, "body": body, "labels": labels or []}},
            )
            return {"created": True, "dry_run": True}
        if not self.token or not self.repository:
            logger.warning("Cannot create issue: missing token/repository.")
            return {"created": False, "error": "missing token or repository"}
        try:
            repo = self._client().get_repo(self.repository)
            issue = repo.create_issue(title=title, body=body, labels=labels or [])
            return {"created": True, "number": issue.number, "url": issue.html_url}
        except Exception as exc:  # pragma: no cover - network/library failure path
            logger.warning("Failed to create issue: %s", exc)
            return {"created": False, "error": str(exc)}

    def find_existing_issue(self, fingerprint: str) -> dict[str, Any] | None:
        """Find an open issue containing the machine-readable finding fingerprint."""
        if self.dry_run or not self.token or not self.repository:
            return None
        try:
            repo = self._client().get_repo(self.repository)
            for issue in repo.get_issues(state="open"):
                if f"evolutionary-cicd:fingerprint={fingerprint}" in (issue.body or ""):
                    return {"number": issue.number, "url": issue.html_url}
        except Exception as exc:  # pragma: no cover - network/library failure path
            logger.warning("Failed to search existing issues: %s", exc)
        return None

    def get_issue(self, number: int) -> Any | None:
        """Fetch an issue through the encapsulated GitHub client."""
        if not self.token or not self.repository:
            return None
        try:
            return self._client().get_repo(self.repository).get_issue(number)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to fetch issue #%s: %s", number, exc)
            return None

    def list_issue_comments(self, number: int) -> list[Any]:
        """List issue comments, returning an empty list on API failure."""
        issue = self.get_issue(number)
        if issue is None:
            return []
        try:
            return list(issue.get_comments())
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to list comments for issue #%s: %s", number, exc)
            return []

    def comment_on_issue(self, number: int, body: str) -> bool:
        """Comment on an issue, honoring dry-run mode."""
        if self.dry_run:
            logger.info(
                "WOULD COMMENT ON ISSUE",
                extra={"extra_fields": {"issue": number, "body": body}},
            )
            return True
        issue = self.get_issue(number)
        if issue is None:
            return False
        try:
            issue.create_comment(body)
            return True
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to comment on issue #%s: %s", number, exc)
            return False

    def assign_issue_to_copilot_result(
        self, number: int, instructions: str | None = None
    ) -> dict[str, Any]:
        """Assign an issue and return the outcome and any API error."""
        if self.dry_run:
            logger.info(
                "WOULD ASSIGN COPILOT",
                extra={"extra_fields": {"issue": number, "instructions": instructions or ""}},
            )
            return {"assigned": True, "assignee": "copilot-swe-agent[bot]", "dry_run": True}
        if not self.copilot_token or not self.repository:
            return {"assigned": False, "error": "missing Copilot token or repository"}
        url = f"https://api.github.com/repos/{self.repository}/issues/{number}/assignees"
        payload = {
            "assignees": ["copilot-swe-agent[bot]"],
            "agent_assignment": {
                "target_repo": self.repository,
                "base_branch": "main",
                "custom_instructions": instructions or "",
                "custom_agent": "",
                "model": "",
            },
        }
        try:
            response = requests.post(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.copilot_token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json=payload,
                timeout=30,
            )
            if response.status_code != 201:
                return {
                    "assigned": False,
                    "assignee": "copilot-swe-agent[bot]",
                    "error": f"GitHub API {response.status_code}: {response.text[:500]}",
                }
            assignees = response.json().get("assignees", [])
            assigned_logins = {assignee.get("login") for assignee in assignees}
            if "copilot-swe-agent" not in assigned_logins:
                return {
                    "assigned": False,
                    "assignee": "copilot-swe-agent[bot]",
                    "error": (
                        "GitHub accepted the request but did not return Copilot " "as an assignee"
                    ),
                }
            return {"assigned": True, "assignee": "copilot-swe-agent[bot]"}
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to assign Copilot to issue #%s: %s", number, exc)
            return {
                "assigned": False,
                "assignee": "copilot-swe-agent[bot]",
                "error": str(exc),
            }

    def assign_issue_to_copilot(self, number: int, instructions: str | None = None) -> bool:
        """Assign an issue using GitHub's supported issue assignee API."""
        return self.assign_issue_to_copilot_result(number, instructions)["assigned"]

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

    def create_pull_request_with_files(
        self,
        branch_name: str,
        base_branch: str,
        title: str,
        body: str,
        files: dict[str, str],
        repository: str | None = None,
    ) -> dict[str, Any]:
        """Commit supplied files on a branch and open a pull request."""
        target_repository = repository or self.repository
        if self.dry_run:
            logger.info(
                "[dry-run] would commit files and create pull request",
                extra={
                    "extra_fields": {
                        "branch": branch_name,
                        "base": base_branch,
                        "files": sorted(files),
                    }
                },
            )
            return {
                "created": False,
                "dry_run": True,
                "branch": branch_name,
                "files": sorted(files),
            }
        if not self.token or not target_repository:
            return {"created": False, "error": "missing GitHub token or repository"}
        try:
            from github import InputGitTreeElement

            repo = self._client().get_repo(target_repository)
            base = repo.get_branch(base_branch)
            repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base.commit.sha)
            tree_elements = []
            for path, content in files.items():
                blob = repo.create_git_blob(content, "utf-8")
                tree_elements.append(
                    InputGitTreeElement(path=path, mode="100644", type="blob", sha=blob.sha)
                )
            tree = repo.create_git_tree(tree_elements, base_tree=base.commit.commit.tree)
            commit = repo.create_git_commit(title, tree, [base.commit.commit])
            repo.get_git_ref(f"heads/{branch_name}").edit(commit.sha)
            pull_request = repo.create_pull(
                title=title,
                body=body,
                head=branch_name,
                base=base_branch,
            )
            return {"created": True, "url": pull_request.html_url, "branch": branch_name}
        except Exception as exc:  # pragma: no cover - network/library failure path
            logger.warning("Failed to create Test Quality pull request: %s", exc)
            return {"created": False, "error": str(exc)}
