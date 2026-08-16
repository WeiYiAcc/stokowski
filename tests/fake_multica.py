#!/usr/bin/env python3
"""Fake `multica` CLI for tests.

Emulates the small subset of `multica issue ...` the MulticaTracker uses,
backed by a JSON "database" file. The state file path comes from the
FAKE_MULTICA_STATE env var; the real CLI binary override is exercised through
the MULTICA_BIN env var that the adapter sets.

State file shape:
{
  "issues": [ {id, identifier, title, description, status, priority, labels,
               parent_issue_id, stage, created_at, updated_at} ],
  "comments": { "<issue_id>": [ {id, content, created_at, author_type} ] },
  "metadata": { "<issue_id>": { "<key>": "<value>" } },
  "next_id": 100,
  "auto_done_after": { "<issue_id>": N },   # after N `issue get` calls, status -> done
  "log": [ "issue status iss-1 done", ... ]
}
"""
from __future__ import annotations

import json
import os
import sys

STATE_PATH = os.environ.get("FAKE_MULTICA_STATE", "")
AUTO_DONE_AFTER = "auto_done_after"
COUNT_KEY = "_get_counts"


def load():
    if not STATE_PATH or not os.path.exists(STATE_PATH):
        return {"issues": [], "comments": {}, "next_id": 100, "log": []}
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def out(obj):
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)


def err(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def arg_value(args, flag, default=None):
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return default


def has_flag(args, flag):
    return flag in args


def find_issue(state, issue_id):
    for issue in state["issues"]:
        if issue["id"] == issue_id:
            return issue
    return None


def get_counts(state):
    return state.setdefault(COUNT_KEY, {})


def bump_get(state, issue_id):
    """Count a `issue get` for this id; flip to done once the threshold hits."""
    counts = get_counts(state)
    target = (state.get(AUTO_DONE_AFTER) or {}).get(issue_id)
    if target is None:
        return
    counts[issue_id] = counts.get(issue_id, 0) + 1
    if counts[issue_id] >= target:
        issue = find_issue(state, issue_id)
        if issue:
            issue["status"] = "done"


def main(argv):
    if not STATE_PATH:
        err("FAKE_MULTICA_STATE not set")
    if not argv or argv[0] != "issue":
        err(f"fake multica: unsupported command: {argv[:2]}")
    sub = argv[1] if len(argv) > 1 else ""
    rest = argv[2:]

    state = load()

    if sub == "list":
        status = arg_value(rest, "--status")
        project = arg_value(rest, "--project")
        limit = int(arg_value(rest, "--limit", "100"))
        offset = int(arg_value(rest, "--offset", "0"))
        issues = []
        for issue in state["issues"]:
            if status and issue.get("status") != status:
                continue
            if project and issue.get("project_id") != project:
                continue
            issues.append(issue)
        page = issues[offset:offset + limit]
        out({
            "has_more": offset + len(page) < len(issues),
            "issues": page,
            "limit": limit,
            "offset": offset,
            "total": len(issues),
        })

    if sub == "children":
        parent_id = rest[0]
        by_stage = {}
        unstaged = []
        for issue in state["issues"]:
            if issue.get("parent_issue_id") != parent_id:
                continue
            stage = issue.get("stage")
            if stage is None:
                unstaged.append(issue)
            else:
                by_stage.setdefault(stage, []).append(issue)
        stages = []
        for stage in sorted(by_stage):
            items = by_stage[stage]
            stages.append({
                "stage": stage,
                "total": len(items),
                "done": sum(
                    1 for i in items if i.get("status") in ("done", "cancelled")
                ),
                "issues": items,
            })
        out({"stages": stages, "total": len(stages) + len(unstaged),
             "unstaged": unstaged})

    if sub == "get":
        issue_id = rest[0]
        issue = find_issue(state, issue_id)
        if issue is None:
            err(f"issue {issue_id} not found")
        bump_get(state, issue_id)
        out(issue)

    if sub == "status":
        issue_id = rest[0]
        status = rest[1]
        issue = find_issue(state, issue_id)
        if issue is None:
            err(f"issue {issue_id} not found")
        issue["status"] = status
        state["log"].append(f"status {issue_id} {status}")
        save(state)
        out({"id": issue_id, "status": status})

    if sub == "comment":
        comment_sub = rest[0]
        rest = rest[1:]
        if comment_sub == "add":
            issue_id = rest[0]
            content = sys.stdin.read() if has_flag(rest, "--content-stdin") else ""
            comment = {
                "id": f"c-{len(state['comments'].get(issue_id, [])) + 1}",
                "content": content,
                "created_at": "2026-08-14T12:00:00Z",
                "author_type": "agent",
            }
            state.setdefault("comments", {}).setdefault(issue_id, []).append(comment)
            state["log"].append(f"comment add {issue_id}")
            save(state)
            out({"id": comment["id"]})
        elif comment_sub == "list":
            issue_id = rest[0]
            out(state.get("comments", {}).get(issue_id, []))
        else:
            err(f"fake multica: unknown comment subcommand {comment_sub}")

    if sub == "create":
        title = arg_value(rest, "--title")
        status = arg_value(rest, "--status", "todo")
        project = arg_value(rest, "--project")
        parent = arg_value(rest, "--parent")
        stage = arg_value(rest, "--stage")
        assignee = arg_value(rest, "--assignee")
        description = sys.stdin.read() if has_flag(rest, "--description-stdin") else ""
        nid = f"sub-{state.get('next_id', 100)}"
        state["next_id"] = state.get("next_id", 100) + 1
        issue = {
            "id": nid,
            "identifier": f"WEI-{nid.split('-')[-1]}",
            "title": title,
            "description": description,
            "status": status,
            "priority": "none",
            "labels": [],
            "parent_issue_id": parent,
            "stage": int(stage) if stage else None,
            "assignee": assignee,
            "project_id": project,
            "created_at": "2026-08-14T12:00:00Z",
            "updated_at": "2026-08-14T12:00:00Z",
        }
        state["issues"].append(issue)
        state["log"].append(f"create {nid} parent={parent} stage={stage} assignee={assignee}")
        save(state)
        out({"id": nid})

    if sub == "metadata":
        meta_sub = rest[0]
        rest = rest[1:]
        if meta_sub == "set":
            issue_id = rest[0]
            key = arg_value(rest, "--key")
            value = arg_value(rest, "--value")
            if not key or value is None:
                err("fake multica: metadata set requires --key and --value")
            issue = find_issue(state, issue_id)
            if issue is None:
                err(f"issue {issue_id} not found")
            state.setdefault("metadata", {}).setdefault(issue_id, {})[key] = value
            state["log"].append(f"metadata set {issue_id} {key}={value}")
            save(state)
            out({key: value})
        elif meta_sub == "delete":
            issue_id = rest[0]
            key = arg_value(rest, "--key")
            state.setdefault("metadata", {}).get(issue_id, {}).pop(key, None)
            save(state)
            out({})
        elif meta_sub == "list":
            issue_id = rest[0]
            out(state.get("metadata", {}).get(issue_id, {}))
        else:
            err(f"fake multica: unknown metadata subcommand {meta_sub}")

    err(f"fake multica: unhandled: {argv}")


if __name__ == "__main__":
    main(sys.argv[1:])
