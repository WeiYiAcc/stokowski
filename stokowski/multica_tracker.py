"""Multica tracker — drop-in replacement for Linear, backed by the multica CLI.

Implements the same 6-method interface as :class:`stokowski.linear.LinearClient`
(fetch_candidate_issues / fetch_issues_by_states / fetch_issue_states_by_ids /
post_comment / fetch_comments / update_issue_state), so the orchestrator can be
switched with ``tracker.kind: multica``.

Every call goes through the ``multica`` CLI with ``--output json``. The CLI is
located via (in order): ``tracker.multica_bin`` config, the ``MULTICA_BIN``
environment variable, or ``multica`` on PATH. Proxies are stripped from the
subprocess env (a stale HTTPS_PROXY pointing at 127.0.0.1 blocks the CLI).

State mapping (Linear/logical names -> Multica statuses):
    todo -> todo, In Progress/active -> in_progress, Human Review/review ->
    in_review, Gate Approved -> in_review, Rework -> blocked,
    Done -> done, Closed/Cancelled -> cancelled.

The gate protocol for Multica is comment-driven: a gate state moves the issue
to ``in_review`` and the human decides by commenting ``approve`` or ``rework``
on the issue (see ``evaluate_gate_decision``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

from .models import Issue
from .config import TrackerConfig

logger = logging.getLogger("stokowski.multica")

# Valid Multica issue statuses (multica issue status --help).
MULTICA_STATUSES = {
    "backlog",
    "todo",
    "in_progress",
    "in_review",
    "done",
    "blocked",
    "cancelled",
}

# Terminal statuses used by wait_for_issue_done / cleanup.
# "in_review" counts as stage completion: Multica agents conventionally set a
# finished sub-issue to in_review (awaiting review) rather than done. For a
# stage sub-issue that is a success — the parent-level gate is where human
# review actually happens.
TERMINAL_STATUSES = {"done", "cancelled", "closed", "in_review"}

# Statuses treated as "stage still in flight" for the idempotency guard. A stage
# sub-issue in any of these is reused on a driver restart instead of spawning a
# duplicate; done/cancelled/blocked/backlog are not (blocked is terminal for the
# purpose of re-dispatch, and backlog means the stage was not really started).
ACTIVE_STATUSES = {"todo", "in_progress", "in_review"}

# Alias map from Linear/logical state names to Multica statuses.
_STATE_ALIASES = {
    "todo": "todo",
    "backlog": "backlog",
    "active": "in_progress",
    "in progress": "in_progress",
    "in_progress": "in_progress",
    "review": "in_review",
    "human review": "in_review",
    "in_review": "in_review",
    "gate approved": "in_review",
    "rework": "blocked",
    "blocked": "blocked",
    "done": "done",
    "terminal": "done",
    "closed": "cancelled",
    "canceled": "cancelled",
    "cancelled": "cancelled",
}

_PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3}

_APPROVE_RE = re.compile(r"\bapprov\w*\b", re.IGNORECASE)
_REWORK_RE = re.compile(r"\brework\w*\b", re.IGNORECASE)


def map_state(state: str) -> str:
    """Map a Linear/logical state name to a Multica status.

    Unknown values are passed through lowercased; anything already a valid
    Multica status is unchanged.
    """
    if not state:
        return ""
    key = str(state).strip().lower()
    if key in MULTICA_STATUSES:
        return key
    return _STATE_ALIASES.get(key, key)


def evaluate_gate_decision(comments: list[dict]) -> str | None:
    """Decide a gate from human comments.

    ``comments`` is a list of normalized comment dicts with at least a ``body``
    key (and optional ``author_type``). Stokowski's own tracking comments are
    skipped (they contain ``<!-- stokowski:``). The most recent comment that
    mentions ``approve`` or ``rework`` decides; if a single comment mentions
    both, ``rework`` wins (safer than advancing without clear approval).

    Returns "approve", "rework", or None if no human signal is present.
    """
    for comment in reversed(list(comments)):
        body = comment.get("body") or ""
        if "<!-- stokowski:" in body:
            continue
        if not body.strip():
            continue
        has_rework = _REWORK_RE.search(body) is not None
        has_approve = _APPROVE_RE.search(body) is not None
        if has_rework:
            return "rework"
        if has_approve:
            return "approve"
    return None


def _normalize_priority(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    text = str(raw).strip().lower()
    if text in _PRIORITY_RANK:
        return _PRIORITY_RANK[text]
    try:
        return int(text)
    except (ValueError, TypeError):
        return None


def _normalize_labels(raw: Any) -> list[str]:
    if not raw:
        return []
    labels: list[str] = []
    for item in raw:
        if isinstance(item, str):
            labels.append(item.lower())
        elif isinstance(item, dict) and item.get("name"):
            labels.append(str(item["name"]).lower())
    return labels


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def normalize_issue(node: dict) -> Issue:
    """Convert a ``multica issue get/list`` node into a Stokowski Issue."""
    return Issue(
        id=node["id"],
        identifier=node.get("identifier") or node.get("number") or node["id"],
        title=node.get("title", ""),
        description=node.get("description"),
        priority=_normalize_priority(node.get("priority")),
        state=map_state(node.get("status", "")),
        labels=_normalize_labels(node.get("labels")),
        url="",
        created_at=_parse_dt(node.get("created_at")),
        updated_at=_parse_dt(node.get("updated_at")),
    )


class MulticaTracker:
    """Multica CLI-backed tracker implementing the LinearClient interface."""

    def __init__(
        self,
        config: TrackerConfig,
        bin_path: str | None = None,
        poll_interval_ms: int = 10_000,
    ):
        self.config = config
        self.provider: dict[str, Any] = config.provider or {}
        self.bin_path = (
            bin_path
            or config.multica_bin
            or os.environ.get("MULTICA_BIN", "")
            or "multica"
        )
        self.poll_interval_ms = int(poll_interval_ms or 10_000)

    # ── Configuration helpers ──────────────────────────────────────────────

    @property
    def project_id(self) -> str:
        """Project UUID used for issue list/create filters."""
        return str(self.provider.get("project_id") or self.config.project_slug or "")

    @property
    def workspace_id(self) -> str:
        return str(self.provider.get("workspace_id") or "")

    @property
    def assignee(self) -> str:
        """Agent (name or UUID) to assign stage sub-issues to."""
        return str(self.provider.get("assignee") or "")

    # ── CLI plumbing ───────────────────────────────────────────────────────

    def _cli_env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in (
            "HTTPS_PROXY",
            "https_proxy",
            "HTTP_PROXY",
            "http_proxy",
            "ALL_PROXY",
            "all_proxy",
        ):
            env.pop(key, None)
        env["MULTICA_BIN"] = self.bin_path
        return env

    def _run_cli(
        self,
        args: list[str],
        input_text: str | None = None,
        timeout_s: int = 60,
    ) -> Any:
        """Run a multica CLI command and parse JSON output.

        Returns the parsed JSON (dict/list/str). Raises RuntimeError on a
        non-zero exit or unparseable JSON.
        """
        cmd = [self.bin_path, *args]
        logger.debug("multica: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                input=input_text,
                capture_output=True,
                text=True,
                env=self._cli_env(),
                timeout=timeout_s,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"multica CLI not found: {self.bin_path} "
                f"(set MULTICA_BIN or tracker.multica_bin)"
            ) from None
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"multica CLI timed out: {' '.join(cmd)}") from None

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise RuntimeError(
                f"multica {' '.join(cmd[:3])} failed (exit {proc.returncode}): "
                f"{stderr[:500] or proc.stdout[:500]}"
            )

        out = (proc.stdout or "").strip()
        if not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return out

    # ── LinearClient interface ─────────────────────────────────────────────

    async def fetch_candidate_issues(
        self, project_slug: str, active_states: list[str]
    ) -> list[Issue]:
        """Return issues in the given active states for the project."""
        return await self._list_issues(active_states)

    async def fetch_issues_by_states(
        self, project_slug: str, states: list[str]
    ) -> list[Issue]:
        """Return issues in the given states (for terminal cleanup)."""
        return await self._list_issues(states)

    async def fetch_issue_states_by_ids(self, ids: list[str]) -> dict[str, str]:
        """Return {id: status} for the given issue IDs."""
        result: dict[str, str] = {}
        for issue_id in ids:
            try:
                node = await self._get_issue(issue_id)
            except Exception as e:
                logger.warning("Failed to fetch state for %s: %s", issue_id, e)
                continue
            if node:
                result[issue_id] = map_state(node.get("status", ""))
        return result

    async def post_comment(self, issue_id: str, body: str) -> bool:
        """Post a comment on a Multica issue via stdin. Returns True on success."""
        try:
            await asyncio.to_thread(
                self._run_cli,
                [
                    "issue",
                    "comment",
                    "add",
                    issue_id,
                    "--content-stdin",
                    "--output",
                    "json",
                ],
                body,
            )
            return True
        except Exception as e:
            logger.error("Failed to post comment on %s: %s", issue_id, e)
            return False

    async def get_issue(self, issue_id: str) -> Issue:
        """Fetch one issue as a normalized Stokowski Issue."""
        node = await self._get_issue(issue_id)
        if not node:
            raise RuntimeError(f"Issue not found: {issue_id}")
        return normalize_issue(node)

    async def fetch_comments(self, issue_id: str) -> list[dict]:
        """Fetch all comments on an issue.

        Returns a list of {id, body, createdAt, author_type} to match the shape
        LinearClient returns ({id, body, createdAt}).
        """
        try:
            data = await asyncio.to_thread(
                self._run_cli,
                ["issue", "comment", "list", issue_id, "--output", "json"],
            )
        except Exception as e:
            logger.error("Failed to fetch comments for %s: %s", issue_id, e)
            return []

        raw_comments = data if isinstance(data, list) else []
        comments = []
        for comment in raw_comments:
            if not isinstance(comment, dict):
                continue
            comments.append(
                {
                    "id": comment.get("id"),
                    "body": comment.get("content", ""),
                    "createdAt": comment.get("created_at"),
                    "author_type": comment.get("author_type"),
                    "author_id": comment.get("author_id"),
                }
            )
        return comments

    async def update_issue_state(self, issue_id: str, state_name: str) -> bool:
        """Move an issue to a new Multica status. Returns True on success."""
        status = map_state(state_name)
        try:
            await asyncio.to_thread(
                self._run_cli,
                ["issue", "status", issue_id, status, "--output", "json"],
            )
            logger.info("Multica tracker: %s → %s", issue_id, status)
            return True
        except Exception as e:
            logger.error("Failed to update state for %s: %s", issue_id, e)
            return False

    async def close(self):
        """No-op — the CLI is invoked per call, there is no connection to close."""

    # ── Multica-specific: stage sub-issue execution ────────────────────────

    async def create_agent_issue(
        self,
        *,
        title: str,
        description: str,
        parent_id: str,
        assignee: str = "",
        stage: int | None = None,
        project_id: str | None = None,
    ) -> str:
        """Create a stage sub-issue assigned to a Multica agent.

        Returns the created issue ID. The sub-issue is created with status
        ``todo`` under ``parent_id`` so it shows up as a child of the parent
        issue and a Multica run records the execution (observability rule:
        Stokowski never spawns an inner agent itself).
        """
        args: list[str] = [
            "issue",
            "create",
            "--title",
            title,
            "--description-stdin",
            "--status",
            "todo",
            "--allow-duplicate",
            "--output",
            "json",
        ]
        pid = project_id or self.project_id
        if pid:
            args += ["--project", pid]
        if parent_id:
            args += ["--parent", parent_id]
        if stage is not None:
            args += ["--stage", str(int(stage))]
        if assignee:
            args += ["--assignee", assignee]

        try:
            data = await asyncio.to_thread(
                self._run_cli, args, description, 90
            )
        except Exception as e:
            logger.error("Failed to create stage sub-issue: %s", e)
            raise RuntimeError(f"Failed to create stage sub-issue: {e}") from None

        node = data.get("issue", data) if isinstance(data, dict) else {}
        issue_id = node.get("id") if isinstance(node, dict) else None
        if not issue_id:
            raise RuntimeError(
                f"multica issue create returned no id: {json.dumps(data)[:300]}"
            )
        logger.info("Created stage sub-issue %s for parent %s", issue_id, parent_id)
        return str(issue_id)

    async def find_stage_subissue(
        self,
        *,
        parent_id: str,
        parent_identifier: str = "",
        state_name: str = "",
        stage: int | None = None,
    ) -> Issue | None:
        """Return an existing, unfinished stage sub-issue under a parent, or None.

        The idempotency guard for ``create_agent_issue``: a driver restart
        resumes the state machine from the parent issue's tracking comments and
        can reach a stage whose previous sub-issue is still in flight, which
        would otherwise spawn a duplicate (WEI-423/WEI-424). Matching is by
        stage ordinal (the ``--stage N`` grouping the ``children`` command
        returns) with a fallback to the title prefix
        ``[{parent_identifier}] {state_name}:`` for unstaged sub-issues. Only
        sub-issues still in flight (todo/in_progress/in_review) count — a
        finished or cancelled one means the stage is done, not pending.
        """
        try:
            data = await asyncio.to_thread(
                self._run_cli,
                ["issue", "children", parent_id, "--output", "json"],
                None,
                60,
            )
        except Exception as e:
            logger.error("Failed to list children of %s: %s", parent_id, e)
            raise RuntimeError(
                f"Failed to list stage sub-issues of {parent_id}: {e}"
            ) from None
        if not isinstance(data, dict):
            return None

        candidates: list[dict] = []
        for group in data.get("stages") or []:
            if isinstance(group, dict):
                candidates.extend(group.get("issues") or [])
        candidates.extend(data.get("unstaged") or [])

        prefix = (
            f"[{parent_identifier}] {state_name}:"
            if parent_identifier and state_name
            else ""
        )
        for node in candidates:
            if not isinstance(node, dict):
                continue
            if map_state(node.get("status", "")) not in ACTIVE_STATUSES:
                continue
            node_stage = node.get("stage")
            if node_stage is not None:
                try:
                    if int(node_stage) == int(stage):
                        return normalize_issue(node)
                except (TypeError, ValueError):
                    pass
            if prefix and str(node.get("title", "")).startswith(prefix):
                return normalize_issue(node)
        return None

    async def wait_for_issue_done(
        self,
        issue_id: str,
        timeout_ms: int = 3_600_000,
        poll_interval_ms: int | None = None,
    ) -> str:
        """Poll a sub-issue until it reaches a terminal state.

        Returns the final status: "done", "in_review", "cancelled", "blocked",
        or "timeout". "in_review" is treated as a successful completion because
        Multica agents end finished sub-issues in in_review.
        """
        interval_s = max((poll_interval_ms or self.poll_interval_ms) / 1000, 2.0)
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            states = await self.fetch_issue_states_by_ids([issue_id])
            status = states.get(issue_id, "")
            if status in TERMINAL_STATUSES:
                logger.info("Stage sub-issue %s reached terminal status %s", issue_id, status)
                return status
            if status == "blocked":
                logger.warning("Stage sub-issue %s is blocked", issue_id)
                return "blocked"
            if time.monotonic() >= deadline:
                logger.warning("Stage sub-issue %s timed out after %sms", issue_id, timeout_ms)
                return "timeout"
            await asyncio.sleep(interval_s)

    # ── Internals ──────────────────────────────────────────────────────────

    async def _get_issue(self, issue_id: str) -> dict | None:
        data = await asyncio.to_thread(
            self._run_cli, ["issue", "get", issue_id, "--output", "json"]
        )
        if isinstance(data, dict) and data.get("id"):
            return data
        return None

    async def _list_issues(self, states: list[str]) -> list[Issue]:
        """List issues for each requested status and merge the results.

        Sub-issues (issues with a parent) are excluded — they are stage-execution
        records the orchestrator creates itself, not top-level workflow
        candidates. Without this filter the orchestrator re-picks its own stage
        sub-issues (they sit in ``todo`` in the same project) and spawns a
        runaway tree of sub-sub-issues.
        """
        seen: dict[str, Issue] = {}
        for raw_state in states or []:
            status = map_state(raw_state)
            if not status:
                continue
            for node in await self._page_issues(status):
                if node.get("parent_issue_id"):
                    continue
                try:
                    issue = normalize_issue(node)
                except (KeyError, TypeError) as e:
                    logger.warning("Skipping malformed issue node: %s", e)
                    continue
                seen[issue.id] = issue
        return list(seen.values())

    async def _page_issues(self, status: str, limit: int = 100) -> list[dict]:
        nodes: list[dict] = []
        offset = 0
        while True:
            args = [
                "issue",
                "list",
                "--status",
                status,
                "--limit",
                str(limit),
                "--offset",
                str(offset),
                "--output",
                "json",
            ]
            if self.project_id:
                args += ["--project", self.project_id]
            try:
                data = await asyncio.to_thread(self._run_cli, args, None, 60)
            except Exception as e:
                logger.warning("Failed to list issues (status=%s): %s", status, e)
                return nodes
            if not isinstance(data, dict):
                break
            page = data.get("issues") or []
            nodes.extend(page)
            if not data.get("has_more") or not page:
                break
            offset += limit
        return nodes
