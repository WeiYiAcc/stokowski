# Implementation

Implement the solution for: {{ issue_title }}

{{ issue_description }}

## Workspace

This is an isolated workspace directory. You have full control.
If the workspace has existing code, read it first and build on it.

## Tournament

Always produce at least two competing implementations per deliverable:

1. `git init` in the workspace root (if not already a repo), then:
   ```
   git worktree add ./variant-a -b variant-a
   git worktree add ./variant-b -b variant-b
   ```
2. Implement each variant independently — different architecture, patterns, or trade-offs.
3. Validate each — type checkers, linters, tests must all pass in both.
4. Merge using tiered resolution:
   - **Tier 1**: clean `git merge` — no conflicts, done.
   - **Tier 2**: auto-resolve — keep the variant with more passing tests.
   - **Tier 3**: cherry-pick the best parts from each into main.
   - **Tier 4**: if irreconcilable, rewrite main incorporating lessons from both.
5. Keep losing branch for reference. Post comparison summary as a comment.

Every deliverable goes through this tournament — single or multi-language.
When the task requires multiple languages or projects, run a separate tournament
for each, then compare the winners.

## Quality gates

Before marking work complete:
- Type checkers pass (tsc / mypy / ruff)
- Tests pass
- No TODO markers remaining
- Changes committed with descriptive messages
