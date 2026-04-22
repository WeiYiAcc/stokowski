"""Local file-based tracker — drop-in replacement for Linear.

Tasks are YAML files in a directory. Each file = one issue.
State is tracked in the YAML itself. Comments go to a .log sidecar.

Usage in workflow.yaml:
    tracker:
      kind: local
      tasks_dir: ~/agent-state/stokowski-tasks

File format (e.g. tasks_dir/task-001.yaml):
    title: "Implement feature X"
    description: "Details here..."
    state: "Todo"
    priority: 1
    labels: []
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .models import Issue

logger = logging.getLogger("stokowski")


class LocalTracker:
    def __init__(self, tasks_dir: str | Path):
        self.tasks_dir = Path(tasks_dir).expanduser().resolve()
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def _load_task(self, path: Path) -> dict[str, Any] | None:
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.warning(f"Failed to load {path}: {e}")
            return None

    def _save_task(self, path: Path, data: dict[str, Any]) -> None:
        path.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False), encoding="utf-8")

    def _task_to_issue(self, path: Path, data: dict[str, Any]) -> Issue:
        task_id = path.stem
        return Issue(
            id=task_id,
            identifier=task_id,
            title=data.get("title", ""),
            description=data.get("description", ""),
            state=data.get("state", "Todo"),
            priority=data.get("priority", 0),
            labels=data.get("labels", []),
            url="",
            branch_name=data.get("branch_name", ""),
        )

    async def fetch_candidate_issues(
        self, project_slug: str, active_states: list[str]
    ) -> list[Issue]:
        """Return issues in active states, sorted by priority then filename."""
        active_lower = [s.strip().lower() for s in active_states]
        issues = []
        for path in sorted(self.tasks_dir.glob("*.yaml")):
            data = self._load_task(path)
            if data is None:
                continue
            state = str(data.get("state", "")).strip()
            if state.lower() in active_lower:
                issues.append(self._task_to_issue(path, data))
        issues.sort(key=lambda i: (i.priority or 999, i.identifier))
        return issues

    async def fetch_issue_states_by_ids(self, ids: list[str]) -> dict[str, str]:
        """Return {id: state} for given issue IDs."""
        result = {}
        for issue_id in ids:
            path = self.tasks_dir / f"{issue_id}.yaml"
            if path.exists():
                data = self._load_task(path)
                if data:
                    result[issue_id] = str(data.get("state", ""))
        return result

    async def fetch_issues_by_states(
        self, project_slug: str, states: list[str]
    ) -> list[Issue]:
        """Return issues in specific states."""
        target_lower = [s.strip().lower() for s in states]
        issues = []
        for path in sorted(self.tasks_dir.glob("*.yaml")):
            data = self._load_task(path)
            if data is None:
                continue
            state = str(data.get("state", "")).strip()
            if state.lower() in target_lower:
                issues.append(self._task_to_issue(path, data))
        return issues

    async def update_issue_state(self, issue_id: str, new_state: str) -> bool:
        """Update task state in YAML file."""
        path = self.tasks_dir / f"{issue_id}.yaml"
        if not path.exists():
            logger.warning(f"Task file not found: {path}")
            return False
        data = self._load_task(path)
        if data is None:
            return False
        data["state"] = new_state
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save_task(path, data)
        logger.info(f"Local tracker: {issue_id} → {new_state}")
        return True

    async def post_comment(self, issue_id: str, body: str) -> bool:
        """Append comment to sidecar .log file."""
        log_path = self.tasks_dir / f"{issue_id}.log"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n--- {ts} ---\n{body}\n")
        return True

    async def fetch_comments(self, issue_id: str) -> list[dict[str, Any]]:
        """Read comments from sidecar .log for tracking recovery."""
        log_path = self.tasks_dir / f"{issue_id}.log"
        if not log_path.exists():
            return []
        text = log_path.read_text(encoding="utf-8")
        comments = []
        for block in text.split("\n--- "):
            block = block.strip()
            if not block:
                continue
            lines = block.splitlines()
            ts_line = lines[0].rstrip(" -")
            body = "\n".join(lines[1:]).strip()
            if body:
                comments.append({
                    "body": body,
                    "createdAt": ts_line,
                })
        return comments
