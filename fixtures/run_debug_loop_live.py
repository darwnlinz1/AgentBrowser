"""
Live OBSERVE..VERIFY episode on corpus case 4 (http500-null-deref).

The candidate fix is an interception rule fulfilling the failing endpoint
with a well-formed body (the same actuator as the interventional runner).
VERIFY passes only if the symptom reproduces before, disappears after, the
failing causal edge is gone, and the result is stable across reruns.

Usage: python -m fixtures.run_debug_loop_live
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_browser.agent_api import AgentBrowserController
from agent_browser.loop_engine import DebugLoop
from agent_browser.network import InterceptRule
from agent_browser.session_registry import find_free_port
from fixtures.fixture_server import FixtureServer


async def main() -> int:
    with FixtureServer() as srv:
        url = srv.case_url("http500-null-deref")
        ctrl = AgentBrowserController(port=find_free_port(), headless=True, profile="debug_loop_live")
        await ctrl.start(initial_url="about:blank")
        try:
            async def fix(c):
                c.network.add_rule(InterceptRule(
                    rule_id="debug-loop-fix",
                    url_pattern=r"/api/failing-endpoint",
                    action="fulfill", mock_status=200,
                    mock_headers={"Content-Type": "application/json"},
                    mock_body=json.dumps({"nonExistent": {"subField": "recovered"}}),
                ))
                await c.network.enable_interception()

            async def clear(c):
                c.network.clear_rules()
                await c.network.disable_interception()

            report = await DebugLoop(ctrl).run_episode(
                url, action="click", target="#go",
                fix_hook=fix, clear_hook=clear, verify_runs=3)
            print(json.dumps({
                "state": report["state"], "verified": report["verified"],
                "gates": report["gates"]["gates"],
                "signature": report["signature"],
                "time_to_root_cause_ms": report["time_to_root_cause_ms"],
                "symptom_after_runs": report["symptom_after_runs"],
            }, indent=2))
            return 0 if report["verified"] else 1
        finally:
            await ctrl.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
