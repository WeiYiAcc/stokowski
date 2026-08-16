# Stokowski HTTP Shell（无状态 HTTP 壳）

> WEI-445：在 VPS 上部署薄 FastAPI/ASGI 壳，包住 S2 的库 API
> （`Orchestrator.tick_once()` / `Orchestrator.advance(issue)`）。
> 各机 agent 通过 HTTP 调用、本地零安装。

## 设计

- **薄壳**：`stokowski/http_shell.py` 是一个 FastAPI 应用，只做 HTTP ⇄ 库 API 的映射。
- **无状态**：状态机状态全部在 Multica issue 里（`<!-- stokowski:* -->` 跟踪标记
  + gate metadata）。服务不落本地状态、无恒常轮询——每次 `advance()` 都从 issue
  重建状态并推进一步；进程重启后从 issue 原样续跑。
- **并发**：进程内共享一个 `Orchestrator`，`/tick` 与 `/advance` 用 asyncio 锁
  串行化（一次只推进一步，避免状态机竞争）。

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 服务可用性 + workflow 加载状态（无需鉴权） |
| GET | `/` | 端点索引 / 调用约定 |
| POST | `/tick` | 跑一次 `tick_once()`（全局扫一遍，返回 dispatch/gates/errors 摘要） |
| POST | `/advance/{issue_id}` | 对指定 issue 推进状态机一步（gate 进 gate/处理响应；agent 状态创建 Multica stage 子 issue 并等其完成后再 transition） |

## 配置

- `STOKOWSKI_WORKFLOW`：workflow 文件路径，默认 `./workflow.yaml`（与仓库
  gitignore 约定一致：workflow 配置为 operator-local，不提交）。
- `STOKOWSKI_HTTP_TOKEN`：可选共享密钥。设置后 `/tick`、`/advance` 需要
  `Authorization: Bearer <token>`（或 `X-API-Key`）；`/health` 保持开放。

## 本地启动（测试）

```bash
uvicorn stokowski.http_shell:app --host 127.0.0.1 --port 8645
```

## VPS 部署（systemd user unit）

参考 `~/.config/systemd/user/multica.service` / `herdr.service` 的管理模式：

```ini
[Unit]
After=network.target multica.service
Description=Stokowski stateless HTTP shell (Multica-driven state machine API)

[Service]
Type=simple
Environment=HOME=%h STOKOWSKI_WORKFLOW=%h/ghq/github.com/WeiYiAcc/stokowski/workflow.yaml STOKOWSKI_HTTP_TOKEN=...
WorkingDirectory=%h/ghq/github.com/WeiYiAcc/stokowski
ExecStart=/bin/bash -lc 'exec %h/ghq/github.com/WeiYiAcc/stokowski/.venv/bin/uvicorn stokowski.http_shell:app --host 127.0.0.1 --port 8645'
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now stokowski-http
systemctl --user status stokowski-http --no-pager
curl -s http://127.0.0.1:8645/health
```

## 调用约定（各机 agent 用 curl 驱动状态机）

```bash
BASE=https://stokowski.wyrunning.dpdns.org   # 或 http://<vps-ip>:8645
TOKEN=...                                    # STOKOWSKI_HTTP_TOKEN 未设置则省略

# 1) 健康检查
curl -s $BASE/health

# 2) 推进一个 issue 的状态机一步（gate 进 gate；agent 状态跑 stage 子 issue）
curl -s -X POST $BASE/advance/<issue-uuid> \
  -H "Authorization: Bearer $TOKEN"

# 3) 全局扫一遍（派发符合条件的新 issue）
curl -s -X POST $BASE/tick -H "Authorization: Bearer $TOKEN"
```

`advance` 响应示例：

```json
{"issue_id": "...", "action": "enter_gate", "state": "research-review", "run": 1}
{"issue_id": "...", "action": "run_stage", "state": "implement", "run": 1,
 "status": "succeeded", "session_id": "sub-xxx", "error": null}
{"issue_id": "...", "action": "none", "state": "done", "reason": "terminal state"}
```
