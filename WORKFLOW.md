---
tracker:
  kind: linear
  project_slug: "5a764322bbe5"
  active_states:
    - Todo
    - In Progress
    - Merging
    - Rework
  terminal_states:
    - Closed
    - Cancelled
    - Canceled
    - Duplicate
    - Done
polling:
  interval_ms: 15000
workspace:
  root: ~/code/stokowski-workspaces
hooks:
  after_create: |
    git clone --depth 1 git@github.com:amadeuscarnegie/cognito-rebuild.git .
  before_run: |
    git pull origin main --rebase 2>/dev/null || true
    echo "=== Pre-turn typecheck ==="
    pnpm typecheck 2>&1 | tail -10
    echo "=== Pre-turn test ==="
    pnpm test 2>&1 | tail -10
  after_run: |
    echo "=== Post-turn quality check ==="
    echo "--- Typecheck ---"
    pnpm typecheck 2>&1 | tail -10
    echo "--- Tests ---"
    pnpm test 2>&1 | tail -10
    echo "--- Custom Lint ---"
    pnpm lint:custom 2>&1 | tail -10
  timeout_ms: 120000
claude:
  permission_mode: auto
  allowed_tools:
    - Bash
    - Read
    - Edit
    - Write
    - Glob
    - Grep
  model: claude-sonnet-4-6
  max_turns: 20
  turn_timeout_ms: 3600000
  stall_timeout_ms: 300000
agent:
  max_concurrent_agents: 3
  max_turns: 20
server:
  port: 4200
---

You are working on a Linear ticket `{{ issue.identifier }}`

{% if attempt %}
Continuation context:

- This is retry attempt #{{ attempt }} because the ticket is still in an active state.
- Resume from the current workspace state instead of restarting from scratch.
- Do not repeat already-completed investigation or validation unless needed for new code changes.
- Do not end the turn while the issue remains in an active state unless you are blocked by missing required permissions/secrets.
{% endif %}

Issue context:
Identifier: {{ issue.identifier }}
Title: {{ issue.title }}
Current status: {{ issue.state }}
Labels: {{ issue.labels }}
URL: {{ issue.url }}

Description:
{% if issue.description %}
{{ issue.description }}
{% else %}
No description provided.
{% endif %}

Instructions:

1. This is an unattended orchestration session. Never ask a human to perform follow-up actions.
2. Only stop early for a true blocker (missing required auth/permissions/secrets). If blocked, record blocker details in a Linear comment.
3. Final message must report completed actions and blockers only.

Work only in the provided repository copy. Do not touch any other path.

## Default posture

- Read and follow the project's CLAUDE.md for coding conventions and standards.
- Read `.claude/rules/agent-pitfalls.md` for known mistakes to avoid.
- Start by determining the ticket's current status, then follow the matching flow.
- Reproduce first: confirm the current behavior/issue before changing code.
- Keep ticket metadata current (state, comments, links).
- Post append-only comments per run. Each run creates new comments; never update a previous run's completion comment. The planning comment for the current run may be updated freely during that run.

## Execution approach

- Spend EXTRA effort on planning and verification. Read all relevant files before writing code.
- When planning: read CLAUDE.md, relevant rule files, the existing code in the area you're modifying, and any related docs.
- When verifying: run all quality commands, review your own diff, check for design token compliance.
- Implementation itself can move quickly once the plan is solid.
- If you've edited the same file more than 3 times for the same issue, STOP. Step back and reconsider your approach.

## Session startup

Before starting any implementation work:
1. Run `pnpm typecheck` to verify the codebase compiles clean.
2. Run `pnpm test` to verify all tests pass.
3. If either fails, investigate and fix before starting new work. Do not build on a broken foundation.
4. Read the ticket description carefully. If it contains acceptance criteria or a criteria JSON block, verify each criterion at the end.

## Status map

- `Todo` -> Move to `In Progress`, then start work.
- `In Progress` -> Implementation actively underway.
- `Human Review` -> PR attached and validated; waiting on human approval.
- `Merging` -> Human approved; merge the PR.
- `Rework` -> Reviewer requested changes; address feedback.
- `Done` -> Terminal; no further action.

## Execution flow

1. Move issue to `In Progress` if in `Todo`.
2. Post a `## Stokowski — Planning [ISO timestamp]` comment on the Linear issue and use it as your scratchpad during this run. You may update this comment freely within the same run.
3. Plan the implementation in the planning comment.
4. Create a feature branch from `main`.
5. Implement the changes with clean, logical commits.
6. Run tests and validation.
7. Update documentation: append to `docs/build-log.md` (what was built), append ADR to `docs/decisions.md` if non-obvious choices were made, run `/update-docs` for other affected docs.
8. Push the branch and create a PR.
9. Link the PR to the Linear issue.
10. Post a NEW `## Stokowski — Completed [ISO timestamp]` comment on the Linear issue. This comment should mirror the PR description: what was built, what was tested, any trade-offs or decisions. Do NOT edit the planning comment — this is a separate new comment.
11. Move issue to `Human Review`.

## Quality bar before Human Review

Before moving to Human Review, you MUST run these commands and verify they pass:

1. `pnpm typecheck` — must exit 0 (zero type errors).
2. `pnpm test` — must exit 0 (all unit + structural tests pass).
3. `pnpm lint:custom` — must exit 0 (no errors; warnings acceptable).

Additional requirements:
- All acceptance criteria from the ticket description met.
- If ticket contains a `criteria` JSON block, verify each criterion and report status.
- PR created, linked to Linear issue, and branch is up to date with `main`.
- Workpad comment updated with: what was done, what was tested, any known limitations.
- `docs/build-log.md` updated with a dated entry of what was built.
- `docs/decisions.md` updated with ADRs for any non-obvious choices.
- If UI changes were made: describe the visual changes in the PR description.

Do NOT move to Human Review if any quality command fails. Fix the issues first.
