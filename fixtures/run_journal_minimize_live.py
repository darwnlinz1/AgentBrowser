"""
Live journal-driven minimization on corpus case 4 (http500-null-deref).

Record a session with the flight-recorder journal on, merge journal
exchanges with network-buffer bodies, then CDD-minimize the exchange set
while hermetic replay keeps reproducing the signature (500 on
/api/failing-endpoint + 'Cannot read properties of undefined').

Expected minimal set: the document + /api/failing-endpoint.
Background pings and the favicon must be minimized away.

Usage: python -m fixtures.run_journal_minimize_live
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_browser.agent_api import AgentBrowserController
from agent_browser.journal import build_exchanges, load_journal
from agent_browser.journal_minimize import merge_bodies, minimize_exchanges
from agent_browser.session_registry import find_free_port
from fixtures.fixture_server import FixtureServer


def _symptom(trace: dict) -> bool:
    exc_ok = any("Cannot read properties of undefined" in str(e.get("description") or e.get("text") or "")
                 for e in trace["runtime"]["exceptions"])
    fail_ok = any(r.get("status") == 500 and "failing-endpoint" in (r.get("url") or "")
                  for r in trace["network"]["requests"])
    return exc_ok and fail_ok


async def main() -> int:
    journal_path = os.path.join(tempfile.mkdtemp(prefix="ab-journal-"), "journal.jsonl")
    os.environ["AGENTBROWSER_JOURNAL"] = journal_path
    with FixtureServer() as srv:
        url = srv.case_url("http500-null-deref")
        ctrl = AgentBrowserController(port=find_free_port(), headless=True, profile="journal_min_live")
        await ctrl.start(initial_url="about:blank")
        try:
            await ctrl.navigate(url)
            trace = await ctrl.tracer.trace_action("click", target="#go")
            assert _symptom(trace), "baseline symptom did not reproduce"
            entries = [dict(e) for e in ctrl.network.requests.values() if e.get("url")]
            await ctrl.stop()

            events = load_journal(journal_path)
            journal_exchanges = build_exchanges(events)
            exchanges = merge_bodies(journal_exchanges, entries)
            print(f"journal events={len(events)} exchanges={len(exchanges)} "
                  f"servable={sum(1 for e in exchanges if e.get('servable'))}")

            # Fresh controller for hermetic minimization runs.
            ctrl2 = AgentBrowserController(port=find_free_port(), headless=True, profile="journal_min_live2")
            await ctrl2.start(initial_url="about:blank")
            try:
                result = await minimize_exchanges(
                    ctrl2, exchanges=exchanges, start_url=url,
                    action="click", target="#go", symptom_check=_symptom,
                    target_url=url.replace("/case/http500-null-deref", "/api/failing-endpoint"),
                )
            finally:
                await ctrl2.stop()
            print(json.dumps(result, indent=2))
            urls = " ".join(result["minimal_urls"])
            ok = ("failing-endpoint" in urls and "background-ping" not in urls
                  and "favicon" not in urls and result["minimal_count"] <= 3)
            print("MINIMIZATION", "PASS" if ok else "NEEDS WORK")
            return 0 if ok else 1
        finally:
            try:
                await ctrl.stop()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
