"""
Live replay-fidelity scoring on real Chrome (loopback fixtures only).

For each seeded case: (1) record phase -- run the ground-truth action
against the live fixture server and snapshot the HTTP responses;
(2) replay phase -- enable hermetic replay from that recording, run the
same action again, and score the replayed responses against the recording
(byte-exact status+body per path), while asserting the fixture server
received ZERO hits during replay (nothing escaped to the live backend).

Usage: python -m fixtures.run_replay_fidelity_live
Exit code 0 only when every case reaches fidelity 1.0 with no escapes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_browser.agent_api import AgentBrowserController
from agent_browser.replay import exchange_from_entry
from agent_browser.session_registry import find_free_port
from fixtures.fault_corpus import CASES
from fixtures.fixture_server import FixtureServer
from fixtures.metrics import replay_fidelity


def _path(url: str) -> str:
    return urlparse(url or "").path


def _responses(ctrl, since_index: int):
    """Comparable (path, status, body) triples from network entries.

    The favicon is browser-internal (requested or cache-served at the
    browser's discretion, not the page's), so it is excluded; every real
    method (GET/PUT/POST...) counts.
    """
    out = []
    order = ctrl.network.traffic_order[since_index:]
    for rid in order:
        entry = ctrl.network.get_request(rid)
        if not entry:
            continue
        path = _path(entry.get("url"))
        if path == "/favicon.ico":
            continue
        status = entry.get("status")
        body = entry.get("response_body")
        if not isinstance(status, int) or body is None or entry.get("body_filtered"):
            continue
        out.append((path, status, body))
    return out


async def _wait_bodies(ctrl, since_index: int, timeout_s: float = 3.0):
    """Body fetches land off the event path; give them a bounded moment."""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pending = False
        for rid in ctrl.network.traffic_order[since_index:]:
            entry = ctrl.network.get_request(rid)
            if entry and isinstance(entry.get("status"), int) \
                    and entry.get("response_body") is None \
                    and not entry.get("body_filtered") and not entry.get("body_error"):
                pending = True
                break
        if not pending:
            return
        await asyncio.sleep(0.05)


async def _run_action(ctrl, case):
    action = case["ground_truth"]["action"]
    if action["type"] == "type":
        return await ctrl.tracer.trace_action("type", target=action["target"], params={"text": "hello"})
    return await ctrl.tracer.trace_action("click", target=action["target"])


async def _run_case(ctrl, srv, case) -> dict:
    url = srv.case_url(case["id"])
    # Mark BEFORE navigating: the document exchange must be part of the
    # recording, or hermetic replay can only serve the stub document.
    mark = len(ctrl.network.traffic_order)
    await ctrl.navigate(url)
    await _run_action(ctrl, case)
    await _wait_bodies(ctrl, mark)
    recorded = _responses(ctrl, mark)
    exchanges = []
    for rid in ctrl.network.traffic_order[mark:]:
        entry = ctrl.network.get_request(rid)
        if entry and entry.get("url"):
            exchanges.append(exchange_from_entry(dict(entry)))

    hits_before = srv.hit_count
    await ctrl.network.enable_hermetic_replay(exchanges)
    try:
        mark2 = len(ctrl.network.traffic_order)
        await ctrl.navigate(url)
        await _run_action(ctrl, case)
        await _wait_bodies(ctrl, mark2)
        replayed = _responses(ctrl, mark2)
    finally:
        await ctrl.network.disable_hermetic_replay()
    escapes = srv.hit_count - hits_before
    score = replay_fidelity(recorded, replayed)
    return {"case": case["id"], "escapes": escapes, **score}


async def main() -> int:
    results = []
    with FixtureServer() as srv:
        ctrl = AgentBrowserController(port=find_free_port(), headless=True,
                                      profile="replay_fidelity", launch_profile="light")
        await ctrl.start(initial_url="about:blank")
        try:
            for case in CASES:
                try:
                    results.append(await _run_case(ctrl, srv, case))
                except Exception as exc:
                    results.append({"case": case["id"], "error": str(exc)[:300]})
        finally:
            await ctrl.stop()

    ok = True
    for r in results:
        if "error" in r:
            ok = False
            print(f"{r['case']:26} ERROR {r['error']}")
            continue
        case_ok = r["fidelity"] == 1.0 and r["escapes"] == 0 and r["recorded"] > 0
        ok = ok and case_ok
        print(f"{r['case']:26} fidelity={r['fidelity']:.4f} "
              f"matched={r['matched']}/{r['recorded']} extra={r['extra_replayed']} escapes={r['escapes']}")
    print(f"\nReplay fidelity: {'PASS' if ok else 'NEEDS WORK'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
