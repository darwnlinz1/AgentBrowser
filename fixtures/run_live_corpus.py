"""
Live per-archetype corpus scoring on real Chrome (loopback fixtures only).

For every seeded case: navigate, run the ground-truth action through the
tracer, then score attribution against ground truth --
  predicted = requests the trace *attributes* to the action (stack-stamped)
  truth     = the request the action truly triggers
Background noise must never be attributed (false-supported target: 0).
Also checks the failing status, the exception signature, and dropped events.

Usage: python -m fixtures.run_live_corpus [--json-out path]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_browser.agent_api import AgentBrowserController
from agent_browser.session_registry import find_free_port
from fixtures.fault_corpus import CASES
from fixtures.fixture_server import FixtureServer
from fixtures.metrics import attribution_scores


def _path(url: str) -> str:
    return urlparse(url or "").path


async def _run_case(ctrl: AgentBrowserController, srv: FixtureServer, case: dict) -> dict:
    gt = case["ground_truth"]
    started = time.perf_counter()
    await ctrl.navigate(srv.case_url(case["id"]))
    action = gt["action"]
    if action["type"] == "type":
        trace = await ctrl.tracer.trace_action("type", target=action["target"], params={"text": "hello"})
    else:
        trace = await ctrl.tracer.trace_action("click", target=action["target"])
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    requests = trace["network"]["requests"]
    predicted = {_path(r["url"]) for r in requests if r.get("confidence") == "attributed"}
    if gt.get("triggered_requests"):
        truth = set(gt["triggered_requests"])
    elif gt.get("triggered_request"):
        truth = {gt["triggered_request"]}
    else:
        truth = set()
    background = set(gt.get("background_requests") or [])
    scores = attribution_scores(predicted, truth)
    false_on_background = sorted(predicted & background)

    failing_seen = [r.get("status") for r in requests if isinstance(r.get("status"), int) and r["status"] >= 400]
    exceptions_text = " ".join(str(e.get("description") or e.get("text") or "") for e in trace["runtime"]["exceptions"])
    return {
        "case": case["id"],
        "elapsed_ms": elapsed_ms,
        "predicted_attributed": sorted(predicted),
        "truth": sorted(truth),
        "precision": scores["precision"],
        "recall": scores["recall"],
        "false_on_background": false_on_background,
        "failing_status_expected": gt.get("failing_status"),
        "failing_status_seen": failing_seen,
        "failing_ok": (gt.get("failing_status") is None) or (gt["failing_status"] in failing_seen),
        "exception_expected": gt.get("exception_signature"),
        "exception_ok": (not gt.get("exception_signature")) or (gt["exception_signature"] in exceptions_text),
        "settle_reason": (trace["evidence"].get("settle") or {}).get("settle_reason"),
        "dropped_events": ctrl.cdp.dropped_events if ctrl.cdp else None,
    }


async def main() -> int:
    json_out = None
    if "--json-out" in sys.argv:
        json_out = sys.argv[sys.argv.index("--json-out") + 1]
    results = []
    with FixtureServer() as srv:
        ctrl = AgentBrowserController(port=find_free_port(), headless=True,
                                      profile="live_corpus", launch_profile="light")
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
    print(f"{'case':26} {'P':>5} {'R':>5} {'false-bg':9} {'fail':5} {'exc':5}")
    for r in results:
        if "error" in r:
            ok = False
            print(f"{r['case']:26} ERROR {r['error']}")
            continue
        case_ok = (r["precision"] == 1.0 and r["recall"] == 1.0 and not r["false_on_background"]
                   and r["failing_ok"] and r["exception_ok"])
        ok = ok and case_ok
        print(f"{r['case']:26} {r['precision']:>5} {r['recall']:>5} {str(bool(r['false_on_background'])):9} "
              f"{str(r['failing_ok']):5} {str(r['exception_ok']):5} settle={r['settle_reason']} dropped={r['dropped_events']}")
    dropped_total = max((r.get("dropped_events") or 0) for r in results) if results else 0
    print(f"\nOverall: {'PASS' if ok else 'NEEDS WORK'} | dropped_events(final)={dropped_total}")
    if json_out:
        with open(json_out, "w", encoding="utf-8") as fh:
            json.dump({"results": results, "pass": ok}, fh, indent=2)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
