"""Thin stateless FastAPI/ASGI shell over the Stokowski library API (WEI-445).

Maps HTTP endpoints onto ``Orchestrator.tick_once()`` / ``Orchestrator.advance(issue)``
so any machine can drive a Stokowski state machine over plain HTTP with zero
local installation.

Statelessness: no local state is persisted and no resident poll loop runs. The
state-machine state lives in the Multica issue (``<!-- stokowski:* -->``
tracking markers + gate metadata, WEI-437). A single shared ``Orchestrator`` is
created lazily per process; every ``advance()`` reconstructs the machine from
the issue, so a process restart picks up exactly where the issue left off.
Requests are serialized with an asyncio lock (one state-machine step at a time).

Endpoints:
    GET  /health                service + workflow load status (open, no auth)
    GET  /                      endpoint index / call convention
    POST /tick                  run one tick_once() sweep
    POST /advance/{issue_id}    advance one issue one step
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .orchestrator import Orchestrator

logger = logging.getLogger("stokowski.http_shell")

app = FastAPI(
    title="Stokowski HTTP Shell",
    description="Stateless HTTP shell over the Stokowski orchestration library.",
    version="0.1.0",
)

# Workflow config path: env STOKOWSKI_WORKFLOW, else ./workflow.yaml in CWD.
DEFAULT_WORKFLOW = Path(os.environ.get("STOKOWSKI_WORKFLOW", "workflow.yaml"))

# Optional shared secret: if STOKOWSKI_HTTP_TOKEN is set, /tick and /advance
# require `Authorization: Bearer <token>` (or X-API-Key). /health stays open.
AUTH_TOKEN = os.environ.get("STOKOWSKI_HTTP_TOKEN", "")

_orchestrator: Orchestrator | None = None
_orchestrator_errors: list[str] = []
_lock = asyncio.Lock()


def get_orchestrator() -> Orchestrator:
    """Return the process-shared Orchestrator (lazy init, hot-reloads workflow)."""
    global _orchestrator, _orchestrator_errors
    if _orchestrator is None:
        orch = Orchestrator(DEFAULT_WORKFLOW)
        _orchestrator_errors = orch._load_workflow()
        if _orchestrator_errors:
            logger.error(
                "Workflow load failed (%s): %s", DEFAULT_WORKFLOW, _orchestrator_errors
            )
        else:
            logger.info(
                "Orchestrator ready: workflow=%s project=%s",
                DEFAULT_WORKFLOW,
                orch.cfg.tracker.provider.get("project_id")
                if orch.cfg
                else "?",
            )
        _orchestrator = orch
    else:
        # Hot-reload: re-validate workflow on each use without discarding state.
        _orchestrator_errors = _orchestrator._load_workflow()
    return _orchestrator


def _check_auth(authorization: str | None, x_api_key: str | None) -> None:
    if not AUTH_TOKEN:
        return
    supplied = x_api_key or ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if supplied != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
async def health() -> JSONResponse:
    """Service availability: workflow load status + basic config info."""
    orch = get_orchestrator()
    status = "ok" if not _orchestrator_errors else "degraded"
    try:
        cfg = orch.cfg
        info = {
            "workflow": str(DEFAULT_WORKFLOW),
            "tracker_kind": cfg.tracker.kind,
            "project_id": cfg.tracker.provider.get("project_id", ""),
            "workspace_id": cfg.tracker.provider.get("workspace_id", ""),
            "entry_state": cfg.entry_state,
        }
    except Exception:
        info = {}
    return JSONResponse(
        {
            "status": status,
            "service": "stokowski-http-shell",
            "version": app.version,
            "errors": _orchestrator_errors,
            **info,
        },
        status_code=200 if status == "ok" else 503,
    )


@app.get("/")
async def index() -> JSONResponse:
    """Endpoint index / call convention."""
    return JSONResponse(
        {
            "service": "stokowski-http-shell",
            "endpoints": {
                "GET /health": "service availability",
                "GET /": "this index",
                "POST /tick": "run one tick_once() sweep",
                "POST /advance/{issue_id}": "advance one issue one step",
            },
            "call_convention": {
                "tick": "curl -X POST <base>/tick",
                "advance": "curl -X POST <base>/advance/<issue-uuid>",
            },
            "auth": "if STOKOWSKI_HTTP_TOKEN is set, add 'Authorization: Bearer <token>'",
            "stateless": "state lives in the Multica issue; no local persistence, no poll loop",
        }
    )


@app.post("/tick")
async def tick(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> JSONResponse:
    """Run one tick_once() sweep. Returns a dispatch/gates/errors summary."""
    _check_auth(authorization, x_api_key)
    orch = get_orchestrator()
    if _orchestrator_errors:
        raise HTTPException(status_code=503, detail={"errors": _orchestrator_errors})
    async with _lock:
        summary: dict[str, Any] = await orch.tick_once()
    return JSONResponse(summary)


@app.post("/advance/{issue_id}")
async def advance(
    issue_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> JSONResponse:
    """Advance one issue one state-machine step.

    Blocks until the step completes:
      - gate state -> enters the gate (or processes a pending response)
      - agent state -> creates a Multica stage sub-issue and waits for it to
        finish, then transitions
    """
    _check_auth(authorization, x_api_key)
    orch = get_orchestrator()
    if _orchestrator_errors:
        raise HTTPException(status_code=503, detail={"errors": _orchestrator_errors})
    async with _lock:
        result: dict[str, Any] = await orch.advance(issue_id)
    return JSONResponse(result)
