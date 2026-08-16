#!/usr/bin/env python3
"""Demo: advance a single Multica issue through the Stokowski state machine.

This script shows the library-API usage introduced in WEI-444:

    Orchestrator.tick_once()  -- single event-driven poll tick
    Orchestrator.advance(issue_id)  -- advance one specific issue by one step

Usage:
    export MULTICA_BIN=/path/to/multica  # optional override
    python examples/advance_demo.py workflow.yaml <issue-id>

The orchestrator reconstructs the issue's internal state from its
``<!-- stokowski:* -->`` tracking comments and ``gate.<state>`` metadata,
then performs the next action (run an agent stage, enter a gate, or process
a gate response).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stokowski.orchestrator import Orchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Advance one Multica issue through a Stokowski workflow."
    )
    parser.add_argument("workflow", help="Path to workflow.yaml")
    parser.add_argument("issue_id", help="Multica issue UUID")
    parser.add_argument(
        "--tick",
        action="store_true",
        help="Run a single tick_once() instead of advance(issue_id)",
    )
    args = parser.parse_args()

    orch = Orchestrator(args.workflow)
    errors = orch._load_workflow()
    if errors:
        for e in errors:
            print(f"Config error: {e}", file=sys.stderr)
        return 1

    if args.tick:
        summary = await orch.tick_once()
        print("tick_once() summary:")
        print(f"  dispatched: {summary['dispatched']}")
        print(f"  errors: {summary['errors']}")
    else:
        result = await orch.advance(args.issue_id)
        print("advance() result:")
        for key, value in result.items():
            print(f"  {key}: {value}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
