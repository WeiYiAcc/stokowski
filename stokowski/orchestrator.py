"""Main orchestration loop - polls Linear, dispatches agents, manages state."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined, TemplateSyntaxError

from .config import (
    ClaudeConfig,
    ServiceConfig,
    StateConfig,
    WorkflowDefinition,
    merge_state_config,
    parse_workflow_file,
    validate_config,
)
from .models import Issue, RetryEntry, RunAttempt
from .prompt import assemble_prompt, build_lifecycle_section
from .tracking import make_gate_comment, make_state_comment, parse_latest_tracking

logger = logging.getLogger("stokowski")


class Orchestrator:
    def __init__(self, workflow_path: str | Path):
        self.workflow_path = Path(workflow_path)
        self.workflow: WorkflowDefinition | None = None

        # Runtime state
        self.running: dict[str, RunAttempt] = {}  # issue_id -> RunAttempt
        self.claimed: set[str] = set()
        self.retry_attempts: dict[str, RetryEntry] = {}
        self.completed: set[str] = set()

        # Aggregate metrics
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_tokens: int = 0
        self.total_seconds_running: float = 0

        # Internal
        self._tracker: Any = None  # MulticaTracker only in this build
        self._tasks: dict[str, asyncio.Task] = {}
        self._retry_timers: dict[str, asyncio.TimerHandle] = {}
        self._last_session_ids: dict[str, str] = {}  # issue_id -> last known stage sub-issue id
        self._jinja = Environment(undefined=StrictUndefined)
        self._running = False
        self._last_issues: dict[str, Issue] = {}
        self._last_completed_at: dict[str, datetime] = {}  # issue_id -> last worker completion time

        # State machine tracking
        self._issue_current_state: dict[str, str] = {}   # issue_id -> internal state name
        self._issue_state_runs: dict[str, int] = {}       # issue_id -> run number for current state
        self._pending_gates: dict[str, str] = {}           # issue_id -> gate state name

    @property
    def cfg(self) -> ServiceConfig:
        assert self.workflow is not None
        return self.workflow.config

    def _load_workflow(self) -> list[str]:
        """Load/reload workflow file. Returns validation errors."""
        try:
            self.workflow = parse_workflow_file(self.workflow_path)
        except Exception as e:
            return [f"Workflow load error: {e}"]
        return validate_config(self.cfg)

    def _ensure_tracker(self):
        """Return the tracker client (Multica only in this build)."""
        if self._tracker is None:
            kind = self.cfg.tracker.kind
            if kind == "multica":
                from .multica_tracker import MulticaTracker
                self._tracker = MulticaTracker(
                    self.cfg.tracker,
                    poll_interval_ms=self.cfg.polling.interval_ms,
                )
            else:
                raise RuntimeError(
                    f"Tracker kind '{kind}' is no longer supported by the built-in orchestrator. "
                    "Use tracker.kind: multica."
                )
        return self._tracker

    def _ensure_linear_client(self):
        """Backward-compatible alias; returns the active tracker."""
        return self._ensure_tracker()

    async def start(self):
        """Start the orchestration loop."""
        errors = self._load_workflow()
        if errors:
            for e in errors:
                logger.error(f"Config error: {e}")
            raise RuntimeError(f"Startup validation failed: {errors}")

        logger.info(
            f"Starting Stokowski "
            f"project={self.cfg.tracker.project_slug} "
            f"max_agents={self.cfg.agent.max_concurrent_agents} "
            f"poll_ms={self.cfg.polling.interval_ms}"
        )

        self._running = True
        self._stop_event = asyncio.Event()

        # Startup terminal cleanup
        await self._startup_cleanup()

        # Main poll loop
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"Tick error: {e}")

            # Interruptible sleep
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.cfg.polling.interval_ms / 1000,
                )
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # Normal poll interval elapsed

    async def stop(self):
        """Stop the orchestration loop and cancel all running stage polls."""
        self._running = False
        if hasattr(self, '_stop_event'):
            self._stop_event.set()

        # Cancel async tasks
        for issue_id, task in list(self._tasks.items()):
            task.cancel()
        # Give them a moment to finish
        if self._tasks:
            await asyncio.sleep(0.5)
        self._tasks.clear()

    async def _startup_cleanup(self):
        """No-op: workspace lifecycle is managed by the executing runner."""
        return

    async def _reconstruct_state(self, issue: Issue) -> tuple[str, int]:
        """Reconstruct the internal state-machine state and run number for an
        issue from its ``<!-- stokowski:* -->`` tracking comments and gate
        metadata.

        Gate metadata (``gate.<state>``) takes precedence over comment parsing
        so that a driver restart can unambiguously reconstruct an
        ``in_review`` issue whose human decision was already recorded (WEI-437).
        """
        client = self._ensure_tracker()
        comments = await client.fetch_comments(issue.id)
        tracking = parse_latest_tracking(comments)

        metadata: dict[str, str] = {}
        if hasattr(client, "get_issue_metadata"):
            try:
                metadata = await client.get_issue_metadata(issue.id)
            except Exception as e:
                logger.warning(
                    "Failed to read metadata for %s: %s", issue.identifier, e
                )

        entry = self.cfg.entry_state
        if entry is None:
            raise RuntimeError("No entry state defined in config")

        if tracking is None:
            return entry, 1

        if tracking.get("type") == "gate":
            gate_state = tracking.get("state", "")
            status = tracking.get("status", "")
            run = tracking.get("run", 1)

            # Metadata is authoritative for completed gate decisions.
            meta_key = f"gate.{gate_state}"
            meta_status = metadata.get(meta_key)
            if meta_status == "approved":
                gate_cfg = self.cfg.states.get(gate_state)
                if gate_cfg and "approve" in gate_cfg.transitions:
                    return gate_cfg.transitions["approve"], run
                return entry, 1
            if meta_status == "rework":
                gate_cfg = self.cfg.states.get(gate_state)
                rework_to = tracking.get("rework_to") or ""
                if not rework_to and gate_cfg:
                    rework_to = gate_cfg.rework_to or ""
                if rework_to and rework_to in self.cfg.states:
                    return rework_to, run + 1
                return entry, 1

            if status == "waiting":
                if gate_state in self.cfg.states:
                    return gate_state, run
                return entry, 1
            if status == "approved":
                gate_cfg = self.cfg.states.get(gate_state)
                if gate_cfg and "approve" in gate_cfg.transitions:
                    return gate_cfg.transitions["approve"], run
                return entry, 1
            if status == "rework":
                gate_cfg = self.cfg.states.get(gate_state)
                rework_to = tracking.get("rework_to") or ""
                if not rework_to and gate_cfg:
                    rework_to = gate_cfg.rework_to or ""
                if rework_to and rework_to in self.cfg.states:
                    return rework_to, run + 1
                return entry, 1

            return entry, 1

        if tracking.get("type") == "state":
            state_name = tracking.get("state", "")
            run = tracking.get("run", 1)
            if state_name in self.cfg.states:
                return state_name, run

        return entry, 1

    async def _resolve_current_state(self, issue: Issue) -> tuple[str, int]:
        """Resolve current state machine state for an issue (cache-aware).
        Returns (state_name, run).
        """
        if issue.id in self._issue_current_state:
            state_name = self._issue_current_state[issue.id]
            run = self._issue_state_runs.get(issue.id, 1)
            return state_name, run

        state_name, run = await self._reconstruct_state(issue)
        self._issue_current_state[issue.id] = state_name
        self._issue_state_runs[issue.id] = run
        return state_name, run

    async def _safe_enter_gate(self, issue: Issue, state_name: str):
        """Wrapper around _enter_gate that logs errors."""
        try:
            await self._enter_gate(issue, state_name)
        except Exception as e:
            logger.error(
                f"Enter gate failed issue={issue.identifier} "
                f"gate={state_name}: {e}",
                exc_info=True,
            )

    async def _enter_gate(self, issue: Issue, state_name: str):
        """Move issue to gate state and post tracking comment."""
        state_cfg = self.cfg.states.get(state_name)
        prompt = state_cfg.prompt if state_cfg else ""
        run = self._issue_state_runs.get(issue.id, 1)

        client = self._ensure_linear_client()

        comment = make_gate_comment(
            state=state_name,
            status="waiting",
            prompt=prompt or "",
            run=run,
        )
        await client.post_comment(issue.id, comment)

        review_state = self.cfg.linear_states.review
        moved = await client.update_issue_state(issue.id, review_state)
        if not moved:
            logger.error(
                f"Failed to move {issue.identifier} to review state '{review_state}' "
                f"— issue will remain claimed to prevent re-dispatch loop"
            )
            # Keep claimed so the issue doesn't get re-dispatched while
            # still in the active Linear state. Track the gate so
            # _handle_gate_responses can pick it up if the state is
            # changed manually.
            self._pending_gates[issue.id] = state_name
            self._issue_current_state[issue.id] = state_name
            self.running.pop(issue.id, None)
            self._tasks.pop(issue.id, None)
            # Schedule a retry to attempt the state move again
            self._schedule_retry(issue, attempt_num=0, delay_ms=10_000)
            return

        self._pending_gates[issue.id] = state_name
        self._issue_current_state[issue.id] = state_name
        # Release from running/claimed so it doesn't block slots
        self.running.pop(issue.id, None)
        self._tasks.pop(issue.id, None)
        self.claimed.discard(issue.id)

        logger.info(
            f"Gate entered issue={issue.identifier} gate={state_name} "
            f"run={run}"
        )

    async def _safe_transition(self, issue: Issue, transition_name: str):
        """Wrapper around _transition that logs errors instead of silently swallowing them."""
        try:
            await self._transition(issue, transition_name)
        except Exception as e:
            logger.error(
                f"Transition failed issue={issue.identifier} "
                f"transition={transition_name}: {e}",
                exc_info=True,
            )
            # Release claimed so the issue can be retried on next tick
            self.claimed.discard(issue.id)

    async def _transition(self, issue: Issue, transition_name: str):
        """Follow a transition from the current state.

        Handles target types:
        - terminal → move to Done, clean workspace, release tracking
        - gate → enter gate
        - agent → post state comment, ensure active Linear state, schedule retry
        """
        current_state_name = self._issue_current_state.get(issue.id)
        if not current_state_name:
            logger.warning(f"No current state for {issue.identifier}, cannot transition")
            return

        current_cfg = self.cfg.states.get(current_state_name)
        if not current_cfg:
            logger.warning(f"Unknown state '{current_state_name}' for {issue.identifier}")
            return

        target_name = current_cfg.transitions.get(transition_name)
        if not target_name:
            logger.warning(
                f"No '{transition_name}' transition from state '{current_state_name}' "
                f"for {issue.identifier}"
            )
            return

        target_cfg = self.cfg.states.get(target_name)
        if not target_cfg:
            logger.warning(f"Transition target '{target_name}' not found in config")
            return

        run = self._issue_state_runs.get(issue.id, 1)

        if target_cfg.type == "terminal":
            # Move issue to terminal state
            terminal_state = self.cfg.terminal_linear_states()[0] if self.cfg.terminal_linear_states() else "Done"
            try:
                client = self._ensure_linear_client()
                moved = await client.update_issue_state(issue.id, terminal_state)
                if moved:
                    logger.info(f"Moved {issue.identifier} to terminal state '{terminal_state}'")
                else:
                    logger.warning(f"Failed to move {issue.identifier} to terminal state '{terminal_state}'")
            except Exception as e:
                logger.warning(f"Failed to move {issue.identifier} to terminal: {e}")
            # Clean up tracking state
            self._issue_current_state.pop(issue.id, None)
            self._issue_state_runs.pop(issue.id, None)
            self._pending_gates.pop(issue.id, None)
            self._last_session_ids.pop(issue.id, None)
            self.claimed.discard(issue.id)
            self.completed.add(issue.id)

        elif target_cfg.type == "gate":
            self._issue_current_state[issue.id] = target_name
            await self._enter_gate(issue, target_name)

        else:
            # Agent state — post state comment, ensure active Linear state, schedule retry
            self._issue_current_state[issue.id] = target_name
            client = self._ensure_linear_client()
            comment = make_state_comment(
                state=target_name,
                run=run,
            )
            await client.post_comment(issue.id, comment)

            # Ensure issue is in active Linear state
            active_state = self.cfg.linear_states.active
            moved = await client.update_issue_state(issue.id, active_state)
            if not moved:
                logger.warning(f"Failed to move {issue.identifier} to active state '{active_state}'")

            self._schedule_retry(issue, attempt_num=0, delay_ms=1000)

    async def _handle_gate_responses(self):
        """Check for gate-approved and rework issues, handle transitions."""
        # Early return if no gate states in config
        has_gates = any(sc.type == "gate" for sc in self.cfg.states.values())
        if not has_gates:
            return

        # Multica uses a comment-driven gate protocol (in_review + approve/rework
        # comment), not Linear's dedicated Gate Approved/Rework states.
        if self.cfg.tracker.kind == "multica":
            await self._handle_multica_gate_responses()
            return

        client = self._ensure_linear_client()

        # Fetch gate-approved issues
        try:
            approved_issues = await client.fetch_issues_by_states(
                self.cfg.tracker.project_slug,
                [self.cfg.linear_states.gate_approved],
            )
        except Exception as e:
            logger.warning(f"Failed to fetch gate-approved issues: {e}")
            approved_issues = []

        for issue in approved_issues:
            if issue.id in self.running or issue.id in self.claimed:
                continue

            gate_state = self._pending_gates.pop(issue.id, None)
            if not gate_state:
                comments = await client.fetch_comments(issue.id)
                tracking = parse_latest_tracking(comments)
                if tracking and tracking.get("type") == "gate" and tracking.get("status") == "waiting":
                    gate_state = tracking.get("state", "")

            if gate_state:
                run = self._issue_state_runs.get(issue.id, 1)
                comment = make_gate_comment(
                    state=gate_state, status="approved", run=run,
                )
                await client.post_comment(issue.id, comment)

                # Follow approve transition
                self._issue_current_state[issue.id] = gate_state
                gate_cfg = self.cfg.states.get(gate_state)
                if gate_cfg and "approve" in gate_cfg.transitions:
                    target = gate_cfg.transitions["approve"]
                    self._issue_current_state[issue.id] = target

                active_state = self.cfg.linear_states.active
                moved = await client.update_issue_state(issue.id, active_state)
                if moved:
                    issue.state = active_state
                else:
                    logger.warning(f"Failed to move {issue.identifier} to active after gate approval")
                self._last_issues[issue.id] = issue
                logger.info(f"Gate approved issue={issue.identifier} gate={gate_state}")

        # Fetch rework issues
        try:
            rework_issues = await client.fetch_issues_by_states(
                self.cfg.tracker.project_slug,
                [self.cfg.linear_states.rework],
            )
        except Exception as e:
            logger.warning(f"Failed to fetch rework issues: {e}")
            rework_issues = []

        for issue in rework_issues:
            if issue.id in self.running or issue.id in self.claimed:
                continue

            gate_state = self._pending_gates.pop(issue.id, None)
            if not gate_state:
                comments = await client.fetch_comments(issue.id)
                tracking = parse_latest_tracking(comments)
                if tracking and tracking.get("type") == "gate" and tracking.get("status") == "waiting":
                    gate_state = tracking.get("state", "")

            if gate_state:
                gate_cfg = self.cfg.states.get(gate_state)
                rework_to = gate_cfg.rework_to if gate_cfg else ""
                if not rework_to:
                    logger.warning(f"Gate {gate_state} has no rework_to target, skipping")
                    continue

                # Check max_rework
                run = self._issue_state_runs.get(issue.id, 1)
                max_rework = gate_cfg.max_rework if gate_cfg else None
                if max_rework is not None and run >= max_rework:
                    # Exceeded max rework — post escalated comment, don't transition
                    comment = make_gate_comment(
                        state=gate_state, status="escalated", run=run,
                    )
                    await client.post_comment(issue.id, comment)
                    logger.warning(
                        f"Max rework exceeded issue={issue.identifier} "
                        f"gate={gate_state} run={run} max={max_rework}"
                    )
                    continue

                new_run = run + 1
                self._issue_state_runs[issue.id] = new_run

                comment = make_gate_comment(
                    state=gate_state, status="rework",
                    rework_to=rework_to, run=new_run,
                )
                await client.post_comment(issue.id, comment)

                self._issue_current_state[issue.id] = rework_to

                active_state = self.cfg.linear_states.active
                moved = await client.update_issue_state(issue.id, active_state)
                if moved:
                    issue.state = active_state
                else:
                    logger.warning(f"Failed to move {issue.identifier} to active after rework")
                self._last_issues[issue.id] = issue
                logger.info(
                    f"Rework issue={issue.identifier} gate={gate_state} "
                    f"rework_to={rework_to} run={new_run}"
                )

    async def _tick(self):
        """Internal backward-compatible tick wrapper."""
        await self.tick_once()

    async def tick_once(self) -> dict[str, Any]:
        """Single event-driven tick without sleeping.

        Reconciles running workers, handles gate responses, fetches candidate
        issues, reconstructs their state machine state, and dispatches eligible
        ones. Returns a summary of actions taken.
        """
        # Reload workflow (supports hot-reload)
        errors = self._load_workflow()

        summary: dict[str, Any] = {
            "dispatched": [],
            "gates_handled": [],
            "errors": [],
        }

        # Part 1: Reconcile running issues
        await self._reconcile()

        # Handle gate responses
        await self._handle_gate_responses()

        # Part 2: Validate config
        if errors:
            summary["errors"].extend(errors)
            logger.warning("Config invalid, skipping dispatch: %s", errors)
            return summary

        # Part 3: Fetch candidates
        try:
            client = self._ensure_linear_client()
            candidates = await client.fetch_candidate_issues(
                self.cfg.tracker.project_slug,
                self.cfg.active_linear_states(),
            )
        except Exception as e:
            logger.error("Failed to fetch candidates: %s", e)
            summary["errors"].append(str(e))
            return summary

        # Cache issues for retry lookup
        for issue in candidates:
            self._last_issues[issue.id] = issue

        # Part 4: Sort by priority
        candidates.sort(
            key=lambda i: (
                i.priority if i.priority is not None else 999,
                i.created_at or datetime.min.replace(tzinfo=timezone.utc),
                i.identifier,
            )
        )

        # Resolve state for new issues before dispatch
        for issue in candidates:
            if issue.id not in self._issue_current_state and issue.id not in self.running:
                try:
                    await self._resolve_current_state(issue)
                except Exception as e:
                    logger.warning(
                        "Failed to resolve state for %s: %s", issue.identifier, e
                    )

        # Part 5: Dispatch
        available_slots = max(
            self.cfg.agent.max_concurrent_agents - len(self.running), 0
        )

        for issue in candidates:
            if available_slots <= 0:
                break
            if not self._is_eligible(issue):
                continue

            # Per-state concurrency check
            state_key = issue.state.strip().lower()
            state_limit = self.cfg.agent.max_concurrent_agents_by_state.get(state_key)
            if state_limit is not None:
                state_count = sum(
                    1
                    for r in self.running.values()
                    if self._last_issues.get(
                        r.issue_id, Issue(id="", identifier="", title="")
                    ).state.strip().lower()
                    == state_key
                )
                if state_count >= state_limit:
                    continue

            self._dispatch(issue)
            summary["dispatched"].append(issue.id)
            available_slots -= 1

        return summary

    async def advance(self, issue_id: str) -> dict[str, Any]:
        """Advance a specific issue by one state-machine step.

        Fetches the issue, reconstructs its internal state from tracking
        comments and gate metadata, then performs the next action:

        - terminal state: no-op
        - gate state: enter the gate (if not pending) or process a pending response
        - agent state: run the stage via a Multica sub-issue and transition on completion

        Returns a dict describing the action taken.
        """
        errors = self._load_workflow()
        if errors:
            raise RuntimeError(f"Startup validation failed: {errors}")

        client = self._ensure_tracker()
        issue = await client.get_issue(issue_id)
        self._last_issues[issue.id] = issue

        # If the latest tracking marker is a waiting gate, process its response
        # (using metadata as authoritative) before falling back to general state
        # reconstruction. This ensures a restart after a metadata write still
        # executes the approve/rework transition.
        comments = await client.fetch_comments(issue_id)
        tracking = parse_latest_tracking(comments)
        if (
            tracking
            and tracking.get("type") == "gate"
            and tracking.get("status") == "waiting"
        ):
            gate_state = tracking.get("state", "")
            gate_cfg = self.cfg.states.get(gate_state)
            if gate_cfg and gate_cfg.type == "gate":
                run = tracking.get("run", 1)
                self._issue_current_state[issue.id] = gate_state
                self._issue_state_runs[issue.id] = run
                self._pending_gates[issue.id] = gate_state
                await self._handle_multica_gate_response_for(issue.id)
                return {
                    "issue_id": issue_id,
                    "action": "check_gate",
                    "state": gate_state,
                    "run": self._issue_state_runs.get(issue.id, run),
                }

        # Force a fresh state resolution (ignore any stale in-memory cache).
        self._issue_current_state.pop(issue.id, None)
        self._issue_state_runs.pop(issue.id, None)

        state_name, run = await self._resolve_current_state(issue)
        state_cfg = self.cfg.states.get(state_name)
        if not state_cfg:
            return {
                "issue_id": issue_id,
                "action": "none",
                "reason": f"unknown state '{state_name}'",
            }

        if state_cfg.type == "terminal":
            return {
                "issue_id": issue_id,
                "action": "none",
                "state": state_name,
                "reason": "terminal state",
            }

        if state_cfg.type == "gate":
            if issue.id not in self._pending_gates:
                await self._enter_gate(issue, state_name)
                return {
                    "issue_id": issue_id,
                    "action": "enter_gate",
                    "state": state_name,
                    "run": run,
                }
            # Already pending — process metadata/comment-driven response.
            await self._handle_multica_gate_response_for(issue.id)
            return {
                "issue_id": issue_id,
                "action": "check_gate",
                "state": state_name,
                "run": self._issue_state_runs.get(issue.id, run),
            }

        # Agent state
        if issue.id in self.running:
            return {
                "issue_id": issue_id,
                "action": "none",
                "reason": "already running",
            }

        self.claimed.add(issue.id)
        attempt = RunAttempt(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            attempt=1,
            state_name=state_name,
        )
        self.running[issue.id] = attempt
        await self._run_worker(issue, attempt)

        return {
            "issue_id": issue_id,
            "action": "run_stage",
            "state": state_name,
            "run": run,
            "status": attempt.status,
            "session_id": attempt.session_id,
            "error": attempt.error,
        }

    def _is_eligible(self, issue: Issue) -> bool:
        """Check if an issue is eligible for dispatch."""
        if not issue.id or not issue.identifier or not issue.title or not issue.state:
            return False

        state_lower = issue.state.strip().lower()
        active_lower = [s.strip().lower() for s in self.cfg.active_linear_states()]
        terminal_lower = [s.strip().lower() for s in self.cfg.terminal_linear_states()]

        if state_lower not in active_lower:
            return False
        if state_lower in terminal_lower:
            return False
        if issue.id in self.running:
            return False
        if issue.id in self.claimed:
            return False

        # Blocker check for Todo
        if state_lower == "todo":
            for blocker in issue.blocked_by:
                if blocker.state and blocker.state.strip().lower() not in terminal_lower:
                    return False

        return True

    def _dispatch(self, issue: Issue, attempt_num: int | None = None):
        """Dispatch a worker for an issue."""
        # Guard against double dispatch: a gate-approval transition schedules a
        # retry (1s) and the same poll tick can also re-pick the now-active
        # issue, which previously spawned duplicate workers and let a stale
        # worker's completion drive an out-of-order transition.
        if issue.id in self.running:
            return
        self.claimed.add(issue.id)

        state_name = self._issue_current_state.get(issue.id)
        if not state_name:
            state_name = self.cfg.entry_state

        # If at a gate, enter it instead of dispatching a worker
        state_cfg = self.cfg.states.get(state_name) if state_name else None
        if state_cfg and state_cfg.type == "gate":
            asyncio.create_task(self._safe_enter_gate(issue, state_name))
            return

        attempt = RunAttempt(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            attempt=attempt_num,
            state_name=state_name,
        )

        # Session handling
        use_fresh_session = False
        if state_cfg and state_cfg.session == "fresh":
            use_fresh_session = True

        if not use_fresh_session:
            if issue.id in self.running:
                old = self.running[issue.id]
                if old.session_id:
                    attempt.session_id = old.session_id
            elif issue.id in self._last_session_ids:
                attempt.session_id = self._last_session_ids[issue.id]

        self.running[issue.id] = attempt
        task = asyncio.create_task(self._run_worker(issue, attempt))
        self._tasks[issue.id] = task

        logger.info(
            f"Dispatched issue={issue.identifier} "
            f"state={issue.state} "
            f"machine_state={state_name or 'entry'} "
            f"channel=multica "
            f"attempt={attempt_num}"
        )

    async def _run_worker(self, issue: Issue, attempt: RunAttempt):
        """Run one agent stage via a Multica sub-issue and transition on completion.

        This is the multica-only replacement for the archived local CLI runner.
        It renders the stage prompt, creates/polls a Multica stage sub-issue,
        and follows the ``complete`` transition when the stage succeeds.
        """
        state_name = attempt.state_name or self.cfg.entry_state
        state_cfg = self.cfg.states.get(state_name) if state_name else None
        claude_cfg = (
            merge_state_config(state_cfg, self.cfg.claude)
            if state_cfg
            else self.cfg.claude
        )

        attempt.status = "streaming"
        attempt.started_at = attempt.started_at or datetime.now(timezone.utc)

        try:
            prompt = await self._render_prompt_async(
                issue, attempt.attempt, state_name
            )
        except Exception as e:
            logger.error(
                "Failed to render prompt for %s: %s", issue.identifier, e
            )
            attempt.status = "failed"
            attempt.error = f"prompt render failed: {e}"
            self._schedule_retry(
                issue, attempt_num=(attempt.attempt or 0) + 1, delay_ms=60_000
            )
            return

        # Multica-only mode: no local workspace, runner CLI, or child PID tracking.
        ws = None

        try:
            result = await self._run_multica_stage(
                issue=issue,
                attempt=attempt,
                state_name=state_name,
                state_cfg=state_cfg,
                claude_cfg=claude_cfg,
                prompt=prompt,
                ws=ws,
            )
        except Exception as e:
            logger.error(
                "Stage failed for %s: %s", issue.identifier, e, exc_info=True
            )
            attempt.status = "failed"
            attempt.error = f"stage exception: {e}"
            self._schedule_retry(
                issue, attempt_num=(attempt.attempt or 0) + 1, delay_ms=60_000
            )
            return

        self.total_input_tokens += result.input_tokens
        self.total_output_tokens += result.output_tokens
        self.total_tokens += result.total_tokens

        if result.status == "succeeded":
            attempt.status = "succeeded"
            attempt.completed_at = datetime.now(timezone.utc)
            self._last_completed_at[issue.id] = attempt.completed_at
            self._last_session_ids[issue.id] = result.session_id
            self.running.pop(issue.id, None)
            self._tasks.pop(issue.id, None)
            self.claimed.discard(issue.id)
            await self._safe_transition(issue, "complete")
        else:
            attempt.status = "failed"
            attempt.error = result.error or "stage failed"
            self._schedule_retry(
                issue, attempt_num=(attempt.attempt or 0) + 1, delay_ms=60_000
            )

    async def _run_multica_stage(
        self,
        issue: Issue,
        attempt: RunAttempt,
        state_name: str,
        state_cfg: StateConfig | None,
        claude_cfg: ClaudeConfig,
        prompt: str,
        ws: Any,
    ) -> RunAttempt:
        """Run one agent stage as a Multica sub-issue (observability rule).

        Stokowski never spawns an inner agent. Each stage becomes a Multica
        sub-issue under the parent issue — a visible run record — assigned to
        a Multica agent (or squad) and the orchestrator only creates it and
        polls until it reaches a terminal state. The runner is fully decoupled:
        the assignee is the state's ``multica_assignee`` (any Multica agent or
        squad) or, when unset, the tracker's provider.assignee.
        """
        tracker = self._ensure_tracker()
        run = self._issue_state_runs.get(issue.id, 1)

        title = f"[{issue.identifier}] {state_name}: {issue.title}"[:200]
        description = self._build_stage_description(
            issue, state_name, state_cfg, run, prompt, ws
        )
        stage = self._stage_ordinal(state_name)
        # Decoupled runner: per-state multica_assignee wins, else the provider
        # default. Works with any Multica agent name/UUID or squad name.
        assignee = (
            state_cfg.multica_assignee
            if state_cfg and getattr(state_cfg, "multica_assignee", "")
            else (getattr(tracker, "assignee", "") or "")
        )

        attempt.status = "streaming"
        attempt.started_at = attempt.started_at or datetime.now(timezone.utc)
        attempt.turn_count += 1
        attempt.last_event_at = datetime.now(timezone.utc)

        # Idempotency guard: a driver restart resumes the state machine from the
        # parent issue's tracking comments and can re-enter a stage whose
        # previous sub-issue is still in flight. Reuse it instead of spawning a
        # duplicate stage sub-issue (the WEI-423/WEI-424 regression).
        try:
            existing = await tracker.find_stage_subissue(
                parent_id=issue.id,
                parent_identifier=issue.identifier,
                state_name=state_name,
                stage=stage,
            )
        except Exception as e:
            # Fail closed: on an uncertain lookup, do not risk a duplicate spawn.
            attempt.status = "failed"
            attempt.error = f"Failed to check for existing stage sub-issue: {e}"
            logger.error(
                "Stage sub-issue dedup check failed issue=%s stage=%s: %s",
                issue.identifier, state_name, e,
            )
            return attempt

        if existing is not None:
            sub_id = existing.id
            logger.info(
                "Reusing existing stage sub-issue %s for %s stage %s",
                sub_id, issue.identifier, state_name,
            )
        else:
            try:
                sub_id = await tracker.create_agent_issue(
                    title=title,
                    description=description,
                    parent_id=issue.id,
                    assignee=assignee,
                    stage=stage,
                )
            except Exception as e:
                attempt.status = "failed"
                attempt.error = f"Failed to create stage sub-issue: {e}"
                logger.error(
                    "Stage sub-issue create failed issue=%s stage=%s: %s",
                    issue.identifier, state_name, e,
                )
                return attempt

        attempt.session_id = sub_id
        attempt.last_message = f"stage sub-issue {sub_id} (run {run})"
        attempt.last_event_at = datetime.now(timezone.utc)

        timeout_ms = claude_cfg.turn_timeout_ms
        final = await tracker.wait_for_issue_done(
            sub_id,
            timeout_ms=timeout_ms,
            poll_interval_ms=self.cfg.polling.interval_ms,
        )

        attempt.last_message = f"stage sub-issue {sub_id} → {final}"
        attempt.last_event_at = datetime.now(timezone.utc)

        # A Multica agent ends a finished stage sub-issue in "done" or, by
        # convention, "in_review" (awaiting review). Both mean the stage's work
        # is complete — human review happens at the parent-level gate.
        if final in ("done", "in_review"):
            attempt.status = "succeeded"
        else:
            attempt.status = "failed"
            attempt.error = f"stage sub-issue {sub_id} ended in '{final}'"
        logger.info(
            "Stage %s issue=%s sub=%s final=%s",
            state_name, issue.identifier, sub_id, final,
        )
        return attempt

    def _stage_ordinal(self, state_name: str) -> int:
        """Return the 1-based stage ordinal for an agent state (for --stage)."""
        agent_states = [
            name for name, sc in self.cfg.states.items() if sc.type == "agent"
        ]
        try:
            return agent_states.index(state_name) + 1
        except ValueError:
            return 1

    def _build_stage_description(
        self,
        issue: Issue,
        state_name: str,
        state_cfg: StateConfig | None,
        run: int,
        prompt: str,
        ws: Any,
    ) -> str:
        """Build a self-contained description for a stage sub-issue."""
        repo = ""
        if ws and getattr(ws, "path", None):
            repo = f"\n- Workspace path: `{ws.path}`"
        extra = ""
        if state_cfg and state_cfg.prompt:
            extra = f"\n- Stage prompt file: `{state_cfg.prompt}`"
        return (
            f"# Stokowski stage: {state_name} (run {run})\n\n"
            f"- Parent issue: {issue.identifier} — {issue.title}\n"
            f"- Machine state: {state_name}\n"
            f"- Stage ordinal: {self._stage_ordinal(state_name)}"
            f"{repo}{extra}\n\n"
            f"## Task\n\n{prompt}\n\n"
            f"## Completion\n"
            f"When the task is finished, reply with a summary and set this issue "
            f"to `done`. If it cannot be completed, set it to `blocked` and "
            f"explain why."
        )

    async def _handle_multica_gate_response_for(self, issue_id: str):
        """Process a gate response for a single issue (Multica protocol).

        Gate metadata (``gate.<state>``) is authoritative when present;
        otherwise the most recent human comment mentioning ``approve``/``rework``
        decides. Rework increments the run counter and returns to ``rework_to``.
        """
        from .tracking import get_comments_since, parse_latest_tracking
        from .multica_tracker import MulticaTracker, evaluate_gate_decision

        gate_state = self._pending_gates.get(issue_id)
        if not gate_state:
            return
        gate_cfg = self.cfg.states.get(gate_state)
        if not gate_cfg or gate_cfg.type != "gate":
            return

        client = self._ensure_linear_client()

        issue = self._last_issues.get(issue_id)
        if issue is None:
            issue = Issue(id=issue_id, identifier=issue_id, title="")
            if isinstance(client, MulticaTracker):
                try:
                    issue = await client.get_issue(issue_id)
                    self._last_issues[issue_id] = issue
                except Exception as e:
                    logger.warning("Failed to fetch gate issue %s: %s", issue_id, e)

        try:
            comments = await client.fetch_comments(issue_id)
        except Exception as e:
            logger.warning("Failed to fetch comments for gate issue %s: %s", issue_id, e)
            return

        tracking = parse_latest_tracking(comments)
        if (
            not tracking
            or tracking.get("type") != "gate"
            or tracking.get("state") != gate_state
            or tracking.get("status") != "waiting"
        ):
            # Not the current waiting gate — leave it alone.
            return

        # Metadata is authoritative for completed gate decisions (WEI-437).
        metadata: dict[str, str] = {}
        if isinstance(client, MulticaTracker):
            try:
                metadata = await client.get_issue_metadata(issue_id)
            except Exception as e:
                logger.warning("Failed to read metadata for gate %s: %s", issue_id, e)

        meta_key = f"gate.{gate_state}"
        meta_status = metadata.get(meta_key)

        since = tracking.get("timestamp")
        recent = get_comments_since(comments, since)
        decision = evaluate_gate_decision(recent)

        if meta_status == "approved":
            decision = "approve"
        elif meta_status == "rework":
            decision = "rework"

        run = self._issue_state_runs.get(issue_id, 1)

        # Record the decision as structured issue metadata so an in_review issue
        # carries an unambiguous, machine-readable outcome (WEI-434).
        if decision in ("approve", "rework") and isinstance(client, MulticaTracker):
            outcome = "approved" if decision == "approve" else "rework"
            try:
                await client.set_issue_metadata(
                    issue_id, f"gate.{gate_state}", outcome
                )
            except Exception as e:
                logger.warning("Failed to write gate metadata for %s: %s", issue_id, e)

        if decision == "approve":
            self._pending_gates.pop(issue_id, None)
            comment = make_gate_comment(
                state=gate_state, status="approved", run=run
            )
            try:
                await client.post_comment(issue_id, comment)
            except Exception as e:
                logger.warning("Failed to post approve comment: %s", e)
            await self._transition(issue, "approve")
            logger.info("Gate approved issue=%s gate=%s", issue.identifier, gate_state)

        elif decision == "rework":
            max_rework = gate_cfg.max_rework
            if max_rework is not None and run >= max_rework:
                comment = make_gate_comment(
                    state=gate_state, status="escalated", run=run
                )
                try:
                    await client.post_comment(issue_id, comment)
                except Exception as e:
                    logger.warning("Failed to post escalated comment: %s", e)
                logger.warning(
                    "Max rework exceeded issue=%s gate=%s run=%s max=%s",
                    issue.identifier, gate_state, run, max_rework,
                )
                return

            rework_to = gate_cfg.rework_to
            new_run = run + 1
            self._pending_gates.pop(issue_id, None)
            self._issue_current_state[issue_id] = rework_to
            self._issue_state_runs[issue_id] = new_run

            comment = make_gate_comment(
                state=gate_state, status="rework",
                rework_to=rework_to, run=new_run,
            )
            try:
                await client.post_comment(issue_id, comment)
            except Exception as e:
                logger.warning("Failed to post rework comment: %s", e)

            active_state = self.cfg.linear_states.active
            moved = await client.update_issue_state(issue_id, active_state)
            if not moved:
                logger.warning(
                    "Failed to move %s to active after rework", issue.identifier
                )
            self._last_issues[issue_id] = issue
            self._schedule_retry(issue, attempt_num=0, delay_ms=1000)
            logger.info(
                "Rework issue=%s gate=%s rework_to=%s run=%s",
                issue.identifier, gate_state, rework_to, new_run,
            )

    async def _handle_multica_gate_responses(self):
        """Process pending gate responses for all tracked Multica gates."""
        for issue_id in list(self._pending_gates):
            await self._handle_multica_gate_response_for(issue_id)

    async def _render_prompt_async(
        self, issue: Issue, attempt_num: int | None, state_name: str | None = None
    ) -> str:
        """Render prompt using state machine prompt assembly (async — fetches comments)."""
        if state_name and state_name in self.cfg.states:
            state_cfg = self.cfg.states[state_name]
            run = self._issue_state_runs.get(issue.id, 1)
            last_completed = self._last_completed_at.get(issue.id)
            last_run_at = last_completed.isoformat() if last_completed else None

            # Fetch comments for lifecycle context
            comments: list[dict] | None = None
            try:
                client = self._ensure_linear_client()
                comments = await client.fetch_comments(issue.id)
            except Exception as e:
                logger.warning(f"Failed to fetch comments for prompt: {e}")

            return assemble_prompt(
                cfg=self.cfg,
                workflow_dir=str(self.workflow_path.parent),
                issue=issue,
                state_name=state_name,
                state_cfg=state_cfg,
                run=run,
                is_rework=False,
                attempt=attempt_num or 1,
                last_run_at=last_run_at,
                comments=comments,
            )

        # Legacy fallback
        return self._render_prompt(issue, attempt_num, state_name)

    def _render_prompt(
        self, issue: Issue, attempt_num: int | None, state_name: str | None = None
    ) -> str:
        """Render the prompt template with issue context (legacy/sync fallback)."""
        assert self.workflow is not None

        # State machine mode: call assemble_prompt without comments
        if state_name and state_name in self.cfg.states:
            state_cfg = self.cfg.states[state_name]
            run = self._issue_state_runs.get(issue.id, 1)
            last_completed = self._last_completed_at.get(issue.id)
            last_run_at = last_completed.isoformat() if last_completed else None

            return assemble_prompt(
                cfg=self.cfg,
                workflow_dir=str(self.workflow_path.parent),
                issue=issue,
                state_name=state_name,
                state_cfg=state_cfg,
                run=run,
                is_rework=False,
                attempt=attempt_num or 1,
                last_run_at=last_run_at,
                comments=None,
            )

        # Legacy mode: use workflow prompt_template with Jinja2
        template_str = self.workflow.prompt_template

        if not template_str:
            return f"You are working on an issue from Linear: {issue.identifier} - {issue.title}"

        last_completed = self._last_completed_at.get(issue.id)
        last_run_at = last_completed.isoformat() if last_completed else ""

        try:
            template = self._jinja.from_string(template_str)
            return template.render(
                issue={
                    "id": issue.id,
                    "identifier": issue.identifier,
                    "title": issue.title,
                    "description": issue.description or "",
                    "priority": issue.priority,
                    "state": issue.state,
                    "branch_name": issue.branch_name,
                    "url": issue.url,
                    "labels": issue.labels,
                    "blocked_by": [
                        {"id": b.id, "identifier": b.identifier, "state": b.state}
                        for b in issue.blocked_by
                    ],
                    "created_at": str(issue.created_at) if issue.created_at else "",
                    "updated_at": str(issue.updated_at) if issue.updated_at else "",
                },
                attempt=attempt_num,
                last_run_at=last_run_at,
                stage=state_name,
            )
        except TemplateSyntaxError as e:
            raise RuntimeError(f"Template syntax error: {e}")

    def _on_agent_event(self, identifier: str, event_type: str, event: dict):
        """Callback for agent events."""
        logger.debug(f"Agent event issue={identifier} type={event_type}")


    def _schedule_retry(
        self,
        issue: Issue,
        attempt_num: int,
        delay_ms: int,
        error: str | None = None,
    ):
        """Schedule a retry for an issue."""
        # Cancel existing retry
        if issue.id in self._retry_timers:
            self._retry_timers[issue.id].cancel()

        entry = RetryEntry(
            issue_id=issue.id,
            identifier=issue.identifier,
            attempt=attempt_num,
            due_at_ms=time.monotonic() * 1000 + delay_ms,
            error=error,
        )
        self.retry_attempts[issue.id] = entry

        loop = asyncio.get_running_loop()
        handle = loop.call_later(
            delay_ms / 1000,
            lambda: loop.create_task(self._handle_retry(issue.id)),
        )
        self._retry_timers[issue.id] = handle

        logger.info(
            f"Retry scheduled issue={issue.identifier} "
            f"attempt={attempt_num} delay={delay_ms}ms "
            f"error={error or 'continuation'}"
        )

    async def _handle_retry(self, issue_id: str):
        """Handle a retry timer firing."""
        entry = self.retry_attempts.pop(issue_id, None)
        self._retry_timers.pop(issue_id, None)

        if entry is None:
            return

        # Fetch fresh candidates to check eligibility
        try:
            client = self._ensure_linear_client()
            candidates = await client.fetch_candidate_issues(
                self.cfg.tracker.project_slug,
                self.cfg.active_linear_states(),
            )
        except Exception as e:
            logger.warning(f"Retry candidate fetch failed: {e}")
            self.claimed.discard(issue_id)
            return

        issue = None
        for c in candidates:
            if c.id == issue_id:
                issue = c
                break

        if issue is None:
            # No longer active
            self.claimed.discard(issue_id)
            logger.info(f"Retry: issue {entry.identifier} no longer active, releasing")
            return

        # Check slots
        available = max(
            self.cfg.agent.max_concurrent_agents - len(self.running), 0
        )
        if available <= 0:
            # Re-queue
            self._schedule_retry(
                issue,
                attempt_num=entry.attempt,
                delay_ms=10_000,
                error="no available orchestrator slots",
            )
            return

        self._dispatch(issue, attempt_num=entry.attempt)

    async def _reconcile(self):
        """Reconcile running issues against current Linear state."""
        if not self.running:
            return

        running_ids = list(self.running.keys())

        try:
            client = self._ensure_linear_client()
            states = await client.fetch_issue_states_by_ids(running_ids)
        except Exception as e:
            logger.warning(f"Reconciliation state fetch failed: {e}")
            return

        terminal_lower = [
            s.strip().lower() for s in self.cfg.terminal_linear_states()
        ]
        active_lower = [
            s.strip().lower() for s in self.cfg.active_linear_states()
        ]
        review_lower = self.cfg.linear_states.review.strip().lower()

        for issue_id in running_ids:
            current_state = states.get(issue_id)
            if current_state is None:
                continue

            state_lower = current_state.strip().lower()

            if state_lower in terminal_lower:
                # Terminal - stop worker (workspace lifecycle is external in multica mode)
                logger.info(
                    f"Reconciliation: {issue_id} is terminal ({current_state}), stopping"
                )
                task = self._tasks.get(issue_id)
                if task:
                    task.cancel()

                self.running.pop(issue_id, None)
                self._tasks.pop(issue_id, None)
                self.claimed.discard(issue_id)

            elif state_lower == review_lower:
                # In review/gate state — stop worker but keep gate tracking
                task = self._tasks.get(issue_id)
                if task:
                    task.cancel()
                self.running.pop(issue_id, None)
                self._tasks.pop(issue_id, None)

            elif state_lower not in active_lower:
                # Neither active nor terminal nor review - stop without cleanup
                logger.info(
                    f"Reconciliation: {issue_id} not active ({current_state}), stopping"
                )
                task = self._tasks.get(issue_id)
                if task:
                    task.cancel()
                self.running.pop(issue_id, None)
                self._tasks.pop(issue_id, None)
                self.claimed.discard(issue_id)

    def get_state_snapshot(self) -> dict[str, Any]:
        """Get current runtime state for observability."""
        now = datetime.now(timezone.utc)
        active_seconds = sum(
            (now - r.started_at).total_seconds()
            for r in self.running.values()
            if r.started_at
        )

        return {
            "generated_at": now.isoformat(),
            "counts": {
                "running": len(self.running),
                "retrying": len(self.retry_attempts),
                "gates": len(self._pending_gates),
            },
            "running": [
                {
                    "issue_id": r.issue_id,
                    "issue_identifier": r.issue_identifier,
                    "session_id": r.session_id,
                    "turn_count": r.turn_count,
                    "status": r.status,
                    "last_event": r.last_event,
                    "last_message": r.last_message,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "last_event_at": (
                        r.last_event_at.isoformat() if r.last_event_at else None
                    ),
                    "tokens": {
                        "input_tokens": r.input_tokens,
                        "output_tokens": r.output_tokens,
                        "total_tokens": r.total_tokens,
                    },
                    "state_name": r.state_name,
                }
                for r in self.running.values()
            ],
            "retrying": [
                {
                    "issue_id": e.issue_id,
                    "issue_identifier": e.identifier,
                    "attempt": e.attempt,
                    "error": e.error,
                }
                for e in self.retry_attempts.values()
            ],
            "gates": [
                {
                    "issue_id": issue_id,
                    "issue_identifier": self._last_issues.get(issue_id, Issue(id="", identifier=issue_id, title="")).identifier,
                    "gate_state": gate_state,
                    "run": self._issue_state_runs.get(issue_id, 1),
                }
                for issue_id, gate_state in self._pending_gates.items()
            ],
            "totals": {
                "input_tokens": self.total_input_tokens,
                "output_tokens": self.total_output_tokens,
                "total_tokens": self.total_tokens,
                "seconds_running": round(
                    self.total_seconds_running + active_seconds, 1
                ),
            },
        }
