WEI-444 完成：编排库 API 化 + 状态从 issue 重建。

## 变更摘要

- **库 API**
  - `Orchestrator.tick_once() -> dict`：单步全局轮询，不 sleep；返回 `dispatched` / `gates_handled` / `errors` 摘要。
  - `Orchestrator.advance(issue_id: str) -> dict`：对指定 issue 单步推进状态机；agent state 会运行 Multica stage sub-issue 并自动 transition，gate state 会进入 gate 或处理 gate 响应。
- **状态重建**
  - 新增 `Orchestrator._reconstruct_state(issue)`，从 `<!-- stokowski:* -->` 跟踪标记 + `gate.<state>` metadata 重建内部状态与 run 计数。
  - `advance()` 对 waiting gate 优先处理 metadata/comment 决策，再回退到通用状态重建。
  - `MulticaTracker.get_issue_metadata()` 新增，fake CLI 同步支持 `metadata list`。
- **multica 单通道清理**
  - 实现之前缺失的 `_run_worker`，直接复用 `_run_multica_stage` 并在成功后 `_transition("complete")`。
  - 移除 S1 遗留的 `remove_workspace` 调用、`_child_pids`、`_linear.close()`、claude runner 日志等无效代码。

## 关键文件

- `stokowski/orchestrator.py`
- `stokowski/multica_tracker.py`
- `tests/test_multica_tracker.py`
- `tests/fake_multica.py`
- `examples/advance_demo.py`

## 单测

```
37 passed in 3.09s
```

原有 30 条全绿，新增 7 条覆盖：

- `get_issue_metadata` / `get_issue_metadata_missing_returns_empty`
- `tick_once_dispatches_eligible_issue`
- `advance_agent_state_creates_subissue_and_moves_to_gate`
- `advance_gate_state_enters_gate`
- `advance_responds_to_metadata_approval`
- `reconstruct_state_from_metadata_rework`

## Demo 运行

```bash
$ uv run python examples/advance_demo.py workflow.yaml iss-1
advance() result:
  issue_id: iss-1
  action: run_stage
  state: investigate
  run: 1
  status: succeeded
  session_id: sub-100
  error: None
```

stage sub-issue 创建、轮询到 done、自动 transition 进入 gate（parent issue 移入 `in_review`）。`--tick` 模式也验证通过。

## 提交

`jj commit` 已推送至 `main`：

```
e51690a2 feat(wei-444): orchestrator library API tick_once/advance + state reconstruction from issue metadata
```
