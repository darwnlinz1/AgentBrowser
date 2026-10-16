"""
Live interventional (counterfactual) confirmation on corpus case 4.

Hypothesis: the 500 on /api/failing-endpoint *causes* the TypeError.
Control runs (no intervention) must reproduce the symptom; treatment runs
(the failing request fulfilled with a well-formed 200 body) must make it
disappear, k-of-k. Only then is the link promoted to `interventional` --
the top rung of the evidence ladder.

Usage: python -m fixtures.run_intervention_live
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_browser.agent_api import AgentBrowserController
from agent_browser.intervention import run_intervention_experiment
from agent_browser.network import InterceptRule
from agent_browser.session_registry import find_free_port
from fixtures.fixture_server import FixtureServer


def _symptom(trace: dict) -> bool:
    exc_ok = any("Cannot read properties of undefined" in str(e.get("description") or e.get("text") or "")
                 for e in trace["runtime"]["exceptions"])
    fail_ok = any(r.get("status") == 500 and "failing-endpoint" in (r.get("url") or "")
                  for r in trace["network"]["requests"])
    return exc_ok and fail_ok


async def main() -> int:
    with FixtureServer() as srv:
        url = srv.case_url("http500-null-deref")
        ctrl = AgentBrowserController(port=find_free_port(), headless=True, profile="intervention_live")
        await ctrl.start(initial_url="about:blank")
        try:
            await ctrl.navigate(url)

            async def trace_once():
                return await ctrl.tracer.trace_action("click", target="#go")

            async def apply():
                ctrl.network.add_rule(InterceptRule(
                    rule_id="intervention-good-response",
                    url_pattern=r"/api/failing-endpoint",
                    action="fulfill", mock_status=200,
                    mock_headers={"Content-Type": "application/json"},
                    mock_body=json.dumps({"nonExistent": {"subField": "recovered"}}),
                ))
                await ctrl.network.enable_interception()

            async def clear():
                ctrl.network.clear_rules()
                await ctrl.network.disable_interception()

            result = await run_intervention_experiment(
                trace_once, apply, clear, symptom_check=_symptom, runs=2)
            print(json.dumps(result, indent=2))
            return 0 if result["promoted"] else 1
        finally:
            await ctrl.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
