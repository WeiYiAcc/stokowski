"""Tests for the Multica tracker adapter (tracker.kind: multica).

Uses a fake ``multica`` CLI script (tests/fake_multica.py) backed by a JSON
state file, so the six LinearClient-interface methods, the comment-driven gate
protocol, and rework counting are all exercised without a live backend.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
from types import SimpleNamespace

import pytest

from stokowski.config import (
    ClaudeConfig,
    TrackerConfig,
    parse_workflow_file,
    validate_config,
)
from stokowski.models import Issue, RunAttempt
from stokowski.multica_tracker import (
    MulticaTracker,
    evaluate_gate_decision,
    map_state,
    normalize_issue,
)
from stokowski.orchestrator import Orchestrator


# ── Fixtures / helpers ────────────────────────────────────────────────────────

@pytest.fixture()
def fake_bin(tmp_path) -> str:
    src = os.path.join(os.path.dirname(__file__), "fake_multica.py")
    path = tmp_path / "multica"
    path.write_text(open(src, encoding="utf-8").read())
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture()
def state_file(tmp_path, monkeypatch) -> str:
    path = str(tmp_path / "state.json")
    monkeypatch.setenv("FAKE_MULTICA_STATE", path)
    return path


def seed(state_path: str, **overrides):
    data = {
        "issues": [],
        "comments": {},
        "next_id": 100,
        "log": [],
        **overrides,
    }
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def read_state(state_path: str) -> dict:
    with open(state_path, encoding="utf-8") as f:
        return json.load(f)


def issue(**kw):
    base = {
        "id": "iss-1",
        "identifier": "WEI-1",
        "title": "Test issue",
        "description": "Do a thing",
        "status": "todo",
        "priority": "high",
        "labels": [],
        "parent_issue_id": None,
        "stage": None,
        "project_id": "proj-1",
        "created_at": "2026-08-14T00:00:00Z",
        "updated_at": "2026-08-14T00:00:00Z",
    }
    base.update(kw)
    return base


def make_tracker(fake_bin: str, state_path: str, **kw) -> MulticaTracker:
    cfg = TrackerConfig(
        kind="multica",
        provider={"project_id": "proj-1", "workspace_id": "ws-1", "assignee": "codex"},
        **kw,
    )
    return MulticaTracker(cfg, bin_path=fake_bin, poll_interval_ms=50)


def run(coro):
    return asyncio.run(coro)


# ── Pure helpers ──────────────────────────────────────────────────────────────

def test_map_state():
    assert map_state("Todo") == "todo"
    assert map_state("In Progress") == "in_progress"
    assert map_state("Human Review") == "in_review"
    assert map_state("Gate Approved") == "in_review"
    assert map_state("Rework") == "blocked"
    assert map_state("Done") == "done"
    assert map_state("Canceled") == "cancelled"
    assert map_state("cancelled") == "cancelled"
    assert map_state("done") == "done"
    assert map_state("Weird State") == "weird state"


def test_evaluate_gate_decision():
    comments = [
        {"body": "<!-- stokowski:gate {} -->\n\nAwaiting review"},
        {"body": "Looks good, approve"},
    ]
    assert evaluate_gate_decision(comments) == "approve"

    assert evaluate_gate_decision([{"body": "needs more work, rework"}]) == "rework"
    # latest comment wins
    assert (
        evaluate_gate_decision(
            [
                {"body": "approve"},
                {"body": "rework please"},
            ]
        )
        == "rework"
    )
    # both in one comment -> rework (safe default)
    assert evaluate_gate_decision([{"body": "approve but rework this part"}]) == "rework"
    # only stokowski tracking comments -> no decision
    assert (
        evaluate_gate_decision([{"body": "<!-- stokowski:gate {} --> x"}]) is None
    )
    assert evaluate_gate_decision([{"body": ""}]) is None
    assert evaluate_gate_decision([]) is None


def test_normalize_issue():
    node = issue(status="in_progress", priority="high", labels=["Backend", "UI"])
    normalized = normalize_issue(node)
    assert normalized.id == "iss-1"
    assert normalized.identifier == "WEI-1"
    assert normalized.state == "in_progress"
    assert normalized.priority == 1
    assert normalized.labels == ["backend", "ui"]


# ── Six-method interface (fake CLI) ───────────────────────────────────────────

def test_fetch_candidate_issues(fake_bin, state_file):
    seed(
        state_file,
        issues=[
            issue(id="a", identifier="WEI-1", status="todo"),
            issue(id="b", identifier="WEI-2", status="in_progress"),
            issue(id="c", identifier="WEI-3", status="done"),
            issue(id="d", identifier="WEI-4", status="cancelled"),
            # sub-issue (has a parent) must NOT be a candidate
            issue(id="e", identifier="WEI-5", status="todo", parent_issue_id="a"),
        ],
    )
    tracker = make_tracker(fake_bin, state_file)
    found = run(tracker.fetch_candidate_issues("proj-1", ["todo", "in_progress"]))
    ids = {i.id for i in found}
    assert ids == {"a", "b"}
    states = {i.id: i.state for i in found}
    assert states == {"a": "todo", "b": "in_progress"}


def test_fetch_issues_by_states(fake_bin, state_file):
    seed(
        state_file,
        issues=[
            issue(id="a", identifier="WEI-1", status="done"),
            issue(id="b", identifier="WEI-2", status="cancelled"),
            issue(id="c", identifier="WEI-3", status="in_progress"),
        ],
    )
    tracker = make_tracker(fake_bin, state_file)
    found = run(tracker.fetch_issues_by_states("proj-1", ["done", "cancelled"]))
    assert {i.id for i in found} == {"a", "b"}


def test_fetch_issue_states_by_ids(fake_bin, state_file):
    seed(
        state_file,
        issues=[
            issue(id="a", status="in_review"),
            issue(id="b", status="done"),
        ],
    )
    tracker = make_tracker(fake_bin, state_file)
    states = run(tracker.fetch_issue_states_by_ids(["a", "b", "missing"]))
    assert states == {"a": "in_review", "b": "done"}


def test_post_comment(fake_bin, state_file):
    seed(state_file, issues=[issue(id="a")])
    tracker = make_tracker(fake_bin, state_file)
    ok = run(tracker.post_comment("a", "hello world"))
    assert ok is True
    data = read_state(state_file)
    assert data["comments"]["a"][-1]["content"] == "hello world"


def test_fetch_comments(fake_bin, state_file):
    seed(
        state_file,
        issues=[issue(id="a")],
        comments={
            "a": [
                {"id": "c1", "content": "one", "created_at": "2026-08-14T00:00:01Z",
                 "author_type": "member"},
                {"id": "c2", "content": "two", "created_at": "2026-08-14T00:00:02Z",
                 "author_type": "agent"},
            ]
        },
    )
    tracker = make_tracker(fake_bin, state_file)
    comments = run(tracker.fetch_comments("a"))
    assert [c["body"] for c in comments] == ["one", "two"]
    assert comments[0]["createdAt"] == "2026-08-14T00:00:01Z"
    assert comments[1]["author_type"] == "agent"


def test_fetch_comments_error(fake_bin, state_file):
    seed(state_file)
    tracker = make_tracker(fake_bin, state_file)
    assert run(tracker.fetch_comments("missing")) == []


def test_update_issue_state_maps_linear_names(fake_bin, state_file):
    seed(state_file, issues=[issue(id="a", status="todo")])
    tracker = make_tracker(fake_bin, state_file)
    assert run(tracker.update_issue_state("a", "Human Review")) is True
    assert read_state(state_file)["issues"][0]["status"] == "in_review"
    assert run(tracker.update_issue_state("a", "Done")) is True
    assert read_state(state_file)["issues"][0]["status"] == "done"


def test_set_issue_metadata(fake_bin, state_file):
    seed(state_file, issues=[issue(id="a")])
    tracker = make_tracker(fake_bin, state_file)
    # write + overwrite both gate outcomes on the same key
    assert run(tracker.set_issue_metadata("a", "gate.research-review", "approved")) is True
    assert read_state(state_file)["metadata"]["a"]["gate.research-review"] == "approved"
    assert run(tracker.set_issue_metadata("a", "gate.research-review", "rework")) is True
    assert read_state(state_file)["metadata"]["a"]["gate.research-review"] == "rework"


def test_create_agent_issue(fake_bin, state_file):
    seed(state_file)
    tracker = make_tracker(fake_bin, state_file)
    sub_id = run(
        tracker.create_agent_issue(
            title="[WEI-1] investigate: T",
            description="Do the work",
            parent_id="iss-1",
            assignee="codex",
            stage=1,
        )
    )
    assert sub_id == "sub-100"
    data = read_state(state_file)
    sub = data["issues"][-1]
    assert sub["parent_issue_id"] == "iss-1"
    assert sub["stage"] == 1
    assert sub["assignee"] == "codex"
    assert sub["status"] == "todo"
    assert "Do the work" in sub["description"]
    assert any("create sub-100 parent=iss-1 stage=1 assignee=codex" in line
               for line in data["log"])


def test_wait_for_issue_done(fake_bin, state_file):
    seed(state_file, issues=[issue(id="done-1", status="done")])
    tracker = make_tracker(fake_bin, state_file)
    assert run(tracker.wait_for_issue_done("done-1", timeout_ms=5000)) == "done"

    # Multica agents end finished sub-issues in in_review -> stage completion
    seed(state_file, issues=[issue(id="rev-1", status="in_review")])
    assert run(tracker.wait_for_issue_done("rev-1", timeout_ms=5000)) == "in_review"

    seed(state_file, issues=[issue(id="blk-1", status="blocked")])
    assert run(tracker.wait_for_issue_done("blk-1", timeout_ms=5000)) == "blocked"

    seed(state_file, issues=[issue(id="td-1", status="todo")])
    assert run(tracker.wait_for_issue_done("td-1", timeout_ms=0)) == "timeout"


def test_find_stage_subissue_returns_active_matching_stage(fake_bin, state_file):
    seed(
        state_file,
        issues=[
            issue(id="iss-1", status="in_progress"),
            # finished sub-issues for stage 1 -> not reusable
            issue(id="sub-1", identifier="WEI-101", status="done",
                  parent_issue_id="iss-1", stage=1),
            # in-flight sub-issue for stage 1 -> reusable
            issue(id="sub-2", identifier="WEI-102", status="in_progress",
                  parent_issue_id="iss-1", stage=1),
            # in-flight but a different stage -> not this stage
            issue(id="sub-3", identifier="WEI-103", status="todo",
                  parent_issue_id="iss-1", stage=2),
        ],
    )
    tracker = make_tracker(fake_bin, state_file)
    found = run(
        tracker.find_stage_subissue(
            parent_id="iss-1",
            parent_identifier="WEI-1",
            state_name="investigate",
            stage=1,
        )
    )
    assert found is not None
    assert found.id == "sub-2"


def test_find_stage_subissue_none_when_all_finished(fake_bin, state_file):
    seed(
        state_file,
        issues=[
            issue(id="iss-1", status="in_progress"),
            issue(id="sub-1", identifier="WEI-101", status="done",
                  parent_issue_id="iss-1", stage=1),
            issue(id="sub-2", identifier="WEI-102", status="cancelled",
                  parent_issue_id="iss-1", stage=1),
            issue(id="sub-3", identifier="WEI-103", status="blocked",
                  parent_issue_id="iss-1", stage=1),
        ],
    )
    tracker = make_tracker(fake_bin, state_file)
    found = run(
        tracker.find_stage_subissue(
            parent_id="iss-1",
            parent_identifier="WEI-1",
            state_name="investigate",
            stage=1,
        )
    )
    assert found is None


def test_find_stage_subissue_matches_unstaged_by_title_prefix(fake_bin, state_file):
    # Legacy/unstaged sub-issues (stage=None) fall back to the title prefix
    # "[<parent identifier>] <state_name>:".
    seed(
        state_file,
        issues=[
            issue(id="iss-1", status="in_progress"),
            issue(id="sub-1", identifier="WEI-101", status="todo",
                  parent_issue_id="iss-1",
                  title="[WEI-1] implement: T"),
        ],
    )
    tracker = make_tracker(fake_bin, state_file)
    found = run(
        tracker.find_stage_subissue(
            parent_id="iss-1",
            parent_identifier="WEI-1",
            state_name="implement",
            stage=2,
        )
    )
    assert found is not None
    assert found.id == "sub-1"


def test_find_stage_subissue_ignores_other_parents(fake_bin, state_file):
    seed(
        state_file,
        issues=[
            issue(id="iss-1", status="in_progress"),
            # in-flight, same stage ordinal + prefix, but under a different parent
            issue(id="sub-1", identifier="WEI-101", status="in_progress",
                  parent_issue_id="other-parent", stage=1,
                  title="[WEI-1] investigate: T"),
        ],
    )
    tracker = make_tracker(fake_bin, state_file)
    found = run(
        tracker.find_stage_subissue(
            parent_id="iss-1",
            parent_identifier="WEI-1",
            state_name="investigate",
            stage=1,
        )
    )
    assert found is None


# ── Idempotency guard: driver restart must not duplicate stage sub-issues ─────


def _stage_orchestrator(tmp_path, fake_bin, state_file):
    wf = _write_workflow(tmp_path, fake_bin)
    orch = _gate_orchestrator(wf)
    orch._issue_state_runs = {"iss-1": 1}
    orch._last_issues = {
        "iss-1": Issue(id="iss-1", identifier="WEI-1", title="T", state="in_progress")
    }
    attempt = RunAttempt(issue_id="iss-1", issue_identifier="WEI-1", state_name="investigate")
    state_cfg = orch.cfg.states["investigate"]
    claude_cfg = ClaudeConfig(turn_timeout_ms=10_000)
    ws = SimpleNamespace(path=str(tmp_path / "ws"))
    return orch, attempt, state_cfg, claude_cfg, ws


def test_multica_run_stage_reuses_existing_subissue(tmp_path, fake_bin, state_file):
    # The WEI-423/WEI-424 regression: after a driver restart, a stage whose
    # previous sub-issue is still in flight must reuse it, not spawn a
    # duplicate. Reuse must also wait for the existing sub-issue to finish.
    seed(
        state_file,
        issues=[
            issue(id="iss-1", status="in_progress"),
            issue(id="sub-100", identifier="WEI-101", status="in_progress",
                  parent_issue_id="iss-1", stage=1,
                  title="[WEI-1] investigate: T"),
        ],
        auto_done_after={"sub-100": 1},
    )
    orch, attempt, state_cfg, claude_cfg, ws = _stage_orchestrator(
        tmp_path, fake_bin, state_file
    )

    result = run(
        orch._run_multica_stage(
            issue=orch._last_issues["iss-1"],
            attempt=attempt,
            state_name="investigate",
            state_cfg=state_cfg,
            claude_cfg=claude_cfg,
            prompt="Do the investigation.",
            ws=ws,
        )
    )
    assert result.status == "succeeded"
    assert result.session_id == "sub-100"
    data = read_state(state_file)
    # no new sub-issue was created
    assert len(data["issues"]) == 2
    assert not any(line.startswith("create ") for line in data["log"])


def test_multica_run_stage_creates_new_when_none_in_flight(tmp_path, fake_bin, state_file):
    # Normal path unchanged: no in-flight sub-issue -> create + poll.
    seed(
        state_file,
        issues=[
            issue(id="iss-1", status="in_progress"),
            # previous run's sub-issue is already done -> create a fresh one
            issue(id="sub-200", identifier="WEI-101", status="done",
                  parent_issue_id="iss-1", stage=1,
                  title="[WEI-1] investigate: T"),
        ],
        auto_done_after={"sub-100": 1},
    )
    orch, attempt, state_cfg, claude_cfg, ws = _stage_orchestrator(
        tmp_path, fake_bin, state_file
    )

    result = run(
        orch._run_multica_stage(
            issue=orch._last_issues["iss-1"],
            attempt=attempt,
            state_name="investigate",
            state_cfg=state_cfg,
            claude_cfg=claude_cfg,
            prompt="Do the investigation.",
            ws=ws,
        )
    )
    assert result.status == "succeeded"
    assert result.session_id == "sub-100"
    data = read_state(state_file)
    assert any(line.startswith("create sub-100") for line in data["log"])


# ── Orchestrator-level: gate protocol + rework counting ───────────────────────

def _write_workflow(tmp_path, fake_bin: str) -> str:
    wf = tmp_path / "workflow.yaml"
    wf.write_text(
        f"""tracker:
  kind: multica
  multica_bin: "{fake_bin}"
  provider:
    project_id: "proj-1"
    assignee: "codex"
linear_states:
  todo: "todo"
  active: "in_progress"
  review: "in_review"
  gate_approved: "in_review"
  rework: "blocked"
  terminal: [done, cancelled]
polling:
  interval_ms: 1000
workspace:
  root: "{tmp_path / 'ws'}"
claude:
  turn_timeout_ms: 10000
agent:
  max_concurrent_agents: 3
states:
  investigate:
    type: agent
    prompt: prompts/investigate.md
    linear_state: active
    runner: codex
    transitions:
      complete: gate
  gate:
    type: gate
    linear_state: review
    rework_to: investigate
    max_rework: 2
    transitions:
      approve: done
  done:
    type: terminal
    linear_state: terminal
""",
        encoding="utf-8",
    )
    return str(wf)


def _gate_orchestrator(wf_path: str):
    orch = Orchestrator(wf_path)
    errors = orch._load_workflow()
    assert not errors, errors
    return orch


def _seed_gate(state_path: str, run: int, decision: str):
    gate_ts = "2026-08-14T10:00:00+00:00"
    human_ts = "2026-08-14T10:00:01Z"
    payload = json.dumps(
        {"state": "gate", "status": "waiting", "run": run, "timestamp": gate_ts}
    )
    comments = [
        {
            "id": "g1",
            "content": f"<!-- stokowski:gate {payload} -->\n\n"
            "**[Stokowski]** Awaiting human review: **gate**",
            "created_at": gate_ts,
            "author_type": "agent",
        }
    ]
    if decision == "rework":
        comments.append(
            {"id": "h1", "content": "please rework this",
             "created_at": human_ts, "author_type": "member"}
        )
    elif decision == "approve":
        comments.append(
            {"id": "h1", "content": "looks good, approve",
             "created_at": human_ts, "author_type": "member"}
        )
    seed(
        state_path,
        issues=[issue(id="iss-1", status="in_review")],
        comments={"iss-1": comments},
    )


def _set_gate_state(orch, run: int):
    orch._pending_gates = {"iss-1": "gate"}
    orch._issue_current_state = {"iss-1": "gate"}
    orch._issue_state_runs = {"iss-1": run}
    orch._last_issues = {
        "iss-1": Issue(id="iss-1", identifier="WEI-1", title="T", state="in_review")
    }


def test_multica_gate_rework_increments_run(tmp_path, fake_bin, state_file):
    wf = _write_workflow(tmp_path, fake_bin)
    _seed_gate(state_file, run=1, decision="rework")
    orch = _gate_orchestrator(wf)
    _set_gate_state(orch, run=1)

    run(orch._handle_multica_gate_responses())

    assert orch._issue_state_runs["iss-1"] == 2
    assert orch._issue_current_state["iss-1"] == "investigate"
    assert "iss-1" not in orch._pending_gates
    data = read_state(state_file)
    assert data["issues"][0]["status"] == "in_progress"
    bodies = [c["content"] for c in data["comments"]["iss-1"]]
    assert any('"status": "rework"' in b and '"run": 2' in b for b in bodies)


def test_multica_gate_escalation_on_max_rework(tmp_path, fake_bin, state_file):
    wf = _write_workflow(tmp_path, fake_bin)
    _seed_gate(state_file, run=2, decision="rework")  # max_rework=2
    orch = _gate_orchestrator(wf)
    _set_gate_state(orch, run=2)

    run(orch._handle_multica_gate_responses())

    assert orch._issue_state_runs["iss-1"] == 2
    assert orch._pending_gates.get("iss-1") == "gate"
    data = read_state(state_file)
    assert data["issues"][0]["status"] == "in_review"
    bodies = [c["content"] for c in data["comments"]["iss-1"]]
    assert any('"status": "escalated"' in b for b in bodies)


def test_multica_gate_approve_moves_to_done(tmp_path, fake_bin, state_file):
    wf = _write_workflow(tmp_path, fake_bin)
    _seed_gate(state_file, run=1, decision="approve")
    orch = _gate_orchestrator(wf)
    _set_gate_state(orch, run=1)

    run(orch._handle_multica_gate_responses())

    assert "iss-1" not in orch._pending_gates
    data = read_state(state_file)
    assert data["issues"][0]["status"] == "done"
    bodies = [c["content"] for c in data["comments"]["iss-1"]]
    assert any('"status": "approved"' in b for b in bodies)


def test_multica_gate_writes_metadata_on_approve(tmp_path, fake_bin, state_file):
    wf = _write_workflow(tmp_path, fake_bin)
    _seed_gate(state_file, run=1, decision="approve")
    orch = _gate_orchestrator(wf)
    _set_gate_state(orch, run=1)

    run(orch._handle_multica_gate_responses())

    data = read_state(state_file)
    assert data["metadata"]["iss-1"]["gate.gate"] == "approved"


def test_multica_gate_writes_metadata_on_rework(tmp_path, fake_bin, state_file):
    wf = _write_workflow(tmp_path, fake_bin)
    _seed_gate(state_file, run=1, decision="rework")
    orch = _gate_orchestrator(wf)
    _set_gate_state(orch, run=1)

    run(orch._handle_multica_gate_responses())

    data = read_state(state_file)
    assert data["metadata"]["iss-1"]["gate.gate"] == "rework"


def test_multica_run_stage_creates_and_polls_subissue(tmp_path, fake_bin, state_file):
    wf = _write_workflow(tmp_path, fake_bin)
    seed(
        state_file,
        issues=[issue(id="iss-1", status="in_progress")],
        auto_done_after={"sub-100": 1},
    )
    orch = _gate_orchestrator(wf)
    orch._issue_state_runs = {"iss-1": 1}
    orch._last_issues = {
        "iss-1": Issue(id="iss-1", identifier="WEI-1", title="T", state="in_progress")
    }
    attempt = RunAttempt(issue_id="iss-1", issue_identifier="WEI-1", state_name="investigate")

    state_cfg = orch.cfg.states["investigate"]
    claude_cfg = ClaudeConfig(turn_timeout_ms=10_000)
    ws = SimpleNamespace(path=str(tmp_path / "ws"))
    result = run(
        orch._run_multica_stage(
            issue=orch._last_issues["iss-1"],
            attempt=attempt,
            state_name="investigate",
            state_cfg=state_cfg,
            claude_cfg=claude_cfg,
            prompt="Do the investigation.",
            ws=ws,
        )
    )

    assert result.status == "succeeded"
    assert result.session_id == "sub-100"
    data = read_state(state_file)
    assert data["issues"][-1]["parent_issue_id"] == "iss-1"
    assert data["issues"][-1]["stage"] == 1
    assert "Do the investigation." in data["issues"][-1]["description"]

def test_multica_run_stage_succeeds_when_subissue_in_review(tmp_path, fake_bin, state_file):
    # A Multica agent ends a finished stage sub-issue in in_review (its
    # convention) — Stokowski must treat that as stage completion.
    wf = _write_workflow(tmp_path, fake_bin)
    seed(
        state_file,
        issues=[
            issue(id="iss-1", status="in_progress"),
            issue(id="sub-100", status="in_review"),
        ],
    )
    orch = _gate_orchestrator(wf)
    orch._issue_state_runs = {"iss-1": 1}
    orch._last_issues = {
        "iss-1": Issue(id="iss-1", identifier="WEI-1", title="T", state="in_progress")
    }
    attempt = RunAttempt(issue_id="iss-1", issue_identifier="WEI-1", state_name="investigate")
    state_cfg = orch.cfg.states["investigate"]
    claude_cfg = ClaudeConfig(turn_timeout_ms=10_000)
    ws = SimpleNamespace(path=str(tmp_path / "ws"))

    result = run(
        orch._run_multica_stage(
            issue=orch._last_issues["iss-1"],
            attempt=attempt,
            state_name="investigate",
            state_cfg=state_cfg,
            claude_cfg=claude_cfg,
            prompt="Do the investigation.",
            ws=ws,
        )
    )
    assert result.status == "succeeded"
    assert result.session_id == "sub-100"

def test_multica_run_stage_per_state_assignee(tmp_path, fake_bin, state_file):
    """The runner is decoupled: a state's multica_assignee selects the Multica
    agent/squad for that stage's sub-issue, overriding provider.assignee."""
    wf = _write_workflow(tmp_path, fake_bin)
    seed(
        state_file,
        issues=[issue(id="iss-1", status="in_progress")],
        auto_done_after={"sub-100": 1},
    )
    orch = _gate_orchestrator(wf)
    orch._issue_state_runs = {"iss-1": 1}
    orch._last_issues = {
        "iss-1": Issue(id="iss-1", identifier="WEI-1", title="T", state="in_progress")
    }
    attempt = RunAttempt(issue_id="iss-1", issue_identifier="WEI-1", state_name="investigate")
    state_cfg = orch.cfg.states["investigate"]
    state_cfg.multica_assignee = "opencode"  # any Multica agent/squad
    claude_cfg = ClaudeConfig(turn_timeout_ms=10_000)
    ws = SimpleNamespace(path=str(tmp_path / "ws"))

    result = run(
        orch._run_multica_stage(
            issue=orch._last_issues["iss-1"],
            attempt=attempt,
            state_name="investigate",
            state_cfg=state_cfg,
            claude_cfg=claude_cfg,
            prompt="Do the investigation.",
            ws=ws,
        )
    )
    assert result.status == "succeeded"
    data = read_state(state_file)
    assert data["issues"][-1]["assignee"] == "opencode"


def test_multica_run_stage_assignee_falls_back_to_provider(tmp_path, fake_bin, state_file):
    wf = _write_workflow(tmp_path, fake_bin)  # provider.assignee = "codex"
    seed(
        state_file,
        issues=[issue(id="iss-1", status="in_progress")],
        auto_done_after={"sub-100": 1},
    )
    orch = _gate_orchestrator(wf)
    orch._issue_state_runs = {"iss-1": 1}
    orch._last_issues = {
        "iss-1": Issue(id="iss-1", identifier="WEI-1", title="T", state="in_progress")
    }
    attempt = RunAttempt(issue_id="iss-1", issue_identifier="WEI-1", state_name="investigate")
    state_cfg = orch.cfg.states["investigate"]  # no multica_assignee set
    claude_cfg = ClaudeConfig(turn_timeout_ms=10_000)
    ws = SimpleNamespace(path=str(tmp_path / "ws"))

    result = run(
        orch._run_multica_stage(
            issue=orch._last_issues["iss-1"],
            attempt=attempt,
            state_name="investigate",
            state_cfg=state_cfg,
            claude_cfg=claude_cfg,
            prompt="Do the investigation.",
            ws=ws,
        )
    )
    assert result.status == "succeeded"
    data = read_state(state_file)
    assert data["issues"][-1]["assignee"] == "codex"


def test_multica_run_stage_failure_when_blocked(tmp_path, fake_bin, state_file):
    wf = _write_workflow(tmp_path, fake_bin)
    seed(
        state_file,
        issues=[issue(id="iss-1", status="in_progress")],
    )
    orch = _gate_orchestrator(wf)
    orch._last_issues = {
        "iss-1": Issue(id="iss-1", identifier="WEI-1", title="T", state="in_progress")
    }
    attempt = RunAttempt(issue_id="iss-1", issue_identifier="WEI-1", state_name="investigate")
    state_cfg = orch.cfg.states["investigate"]
    # zero timeout -> wait_for_issue_done returns "timeout" immediately
    claude_cfg = ClaudeConfig(turn_timeout_ms=0)
    ws = SimpleNamespace(path=str(tmp_path / "ws"))

    result = run(
        orch._run_multica_stage(
            issue=orch._last_issues["iss-1"],
            attempt=attempt,
            state_name="investigate",
            state_cfg=state_cfg,
            claude_cfg=claude_cfg,
            prompt="Do the investigation.",
            ws=ws,
        )
    )
    assert result.status == "failed"
    assert result.session_id == "sub-100"


# ── Config validation ─────────────────────────────────────────────────────────

def test_validate_config_multica_requires_project_id(tmp_path):
    wf = tmp_path / "wf.yaml"
    wf.write_text(
        """tracker:
  kind: multica
  provider: {}
linear_states:
  terminal: [done]
states:
  work:
    type: agent
    prompt: p.md
    transitions:
      complete: done
  done:
    type: terminal
    linear_state: terminal
""",
        encoding="utf-8",
    )
    workflow = parse_workflow_file(str(wf))
    errors = validate_config(workflow.config)
    assert any("project_id" in e for e in errors)


# ── Library API: tick_once / advance ──────────────────────────────────────────


def test_get_issue_metadata(fake_bin, state_file):
    seed(
        state_file,
        issues=[issue(id="a")],
        metadata={"a": {"gate.research-review": "approved", "foo": "bar"}},
    )
    tracker = make_tracker(fake_bin, state_file)
    data = run(tracker.get_issue_metadata("a"))
    assert data == {"gate.research-review": "approved", "foo": "bar"}


def test_get_issue_metadata_missing_returns_empty(fake_bin, state_file):
    seed(state_file, issues=[issue(id="a")])
    tracker = make_tracker(fake_bin, state_file)
    assert run(tracker.get_issue_metadata("a")) == {}


def test_tick_once_dispatches_eligible_issue(tmp_path, fake_bin, state_file):
    wf = _write_workflow(tmp_path, fake_bin)
    seed(
        state_file,
        issues=[issue(id="iss-1", status="in_progress")],
        auto_done_after={"sub-100": 1},
    )
    orch = _gate_orchestrator(wf)

    async def _run():
        summary = await orch.tick_once()
        # Wait for the background _run_worker to finish polling the sub-issue.
        for _ in range(200):
            if not orch.running:
                break
            await asyncio.sleep(0.01)
        return summary

    summary = run(_run())
    assert "iss-1" in summary["dispatched"]
    data = read_state(state_file)
    assert any(line.startswith("create sub-100") for line in data["log"])


def test_advance_agent_state_creates_subissue_and_moves_to_gate(
    tmp_path, fake_bin, state_file
):
    wf = _write_workflow(tmp_path, fake_bin)
    seed(
        state_file,
        issues=[issue(id="iss-1", status="in_progress")],
        auto_done_after={"sub-100": 1},
    )
    orch = _gate_orchestrator(wf)

    result = run(orch.advance("iss-1"))

    assert result["action"] == "run_stage"
    assert result["state"] == "investigate"
    assert result["status"] == "succeeded"
    data = read_state(state_file)
    assert data["issues"][-1]["parent_issue_id"] == "iss-1"
    assert data["issues"][0]["status"] == "in_review"
    assert orch._issue_current_state["iss-1"] == "gate"


def test_advance_gate_state_enters_gate(tmp_path, fake_bin, state_file):
    wf = _write_workflow(tmp_path, fake_bin)
    # A state-tracking marker at the gate (not a waiting gate marker) means the
    # gate has been reached but not yet entered.
    ts = "2026-08-14T10:00:00+00:00"
    payload = json.dumps({"state": "gate", "run": 1, "timestamp": ts})
    comments = [
        {
            "id": "s1",
            "content": f"<!-- stokowski:state {payload} -->\n\nReached gate",
            "created_at": ts,
            "author_type": "agent",
        }
    ]
    seed(state_file, issues=[issue(id="iss-1", status="in_progress")], comments={"iss-1": comments})
    orch = _gate_orchestrator(wf)

    result = run(orch.advance("iss-1"))

    assert result["action"] == "enter_gate"
    assert result["state"] == "gate"
    data = read_state(state_file)
    assert data["issues"][0]["status"] == "in_review"
    bodies = [c["content"] for c in data["comments"]["iss-1"]]
    assert any('"status": "waiting"' in b for b in bodies)


def test_advance_responds_to_metadata_approval(tmp_path, fake_bin, state_file):
    wf = _write_workflow(tmp_path, fake_bin)
    gate_ts = "2026-08-14T10:00:00+00:00"
    payload = json.dumps(
        {"state": "gate", "status": "waiting", "run": 1, "timestamp": gate_ts}
    )
    comments = [
        {
            "id": "g1",
            "content": f"<!-- stokowski:gate {payload} -->\n\nAwaiting review",
            "created_at": gate_ts,
            "author_type": "agent",
        }
    ]
    seed(
        state_file,
        issues=[issue(id="iss-1", status="in_review")],
        comments={"iss-1": comments},
        metadata={"iss-1": {"gate.gate": "approved"}},
    )
    orch = _gate_orchestrator(wf)

    result = run(orch.advance("iss-1"))

    assert result["action"] == "check_gate"
    data = read_state(state_file)
    assert data["issues"][0]["status"] == "done"
    bodies = [c["content"] for c in data["comments"]["iss-1"]]
    assert any('"status": "approved"' in b for b in bodies)


def test_reconstruct_state_from_metadata_rework(tmp_path, fake_bin, state_file):
    wf = _write_workflow(tmp_path, fake_bin)
    gate_ts = "2026-08-14T10:00:00+00:00"
    payload = json.dumps(
        {"state": "gate", "status": "waiting", "run": 1, "timestamp": gate_ts}
    )
    comments = [
        {
            "id": "g1",
            "content": f"<!-- stokowski:gate {payload} -->\n\nAwaiting review",
            "created_at": gate_ts,
            "author_type": "agent",
        }
    ]
    seed(
        state_file,
        issues=[issue(id="iss-1", status="in_review")],
        comments={"iss-1": comments},
        metadata={"iss-1": {"gate.gate": "rework"}},
    )
    orch = _gate_orchestrator(wf)

    state_name, run_number = run(
        orch._reconstruct_state(normalize_issue(issue(id="iss-1", status="in_review")))
    )

    assert state_name == "investigate"
    assert run_number == 2
