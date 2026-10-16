"""
Competitor benchmark harness (AgentBrowser vs Playwright/Puppeteer/Crawl4AI).

Honesty rule: a competitor is only reported as ``measured`` when it is actually
installed and run here. Anything else is ``not_installed`` with the install
hint -- never an estimated or borrowed number.

Measured vectors (the HYPER-BROWSER north stars):
- cold start (ms) to a controllable browser
- action-graph tokens per page (lower is better)
- CDP codec throughput (frames/sec, in-process)
- ring-buffer throughput (events/sec, in-process)
- browser process-tree RAM (MB, live runs via psutil when available)
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import platform
import time
from typing import Any, Dict, Optional

from agent_browser import codec
from agent_browser.axtree import build_ref_table, estimate_tokens, render_view
from agent_browser.ringbuffer import EventRingBuffer


def _timeit(fn, iterations: int) -> float:
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    elapsed = time.perf_counter() - start
    return round(iterations / elapsed, 1) if elapsed else float("inf")


def bench_codec(iterations: int = 20000) -> Dict[str, Any]:
    frame = {"id": 42, "method": "Network.requestWillBeSent",
             "params": {"request": {"url": "https://example.com/api/" + "x" * 120, "method": "GET"},
                        "initiator": {"type": "script", "stack": {"callFrames": [{"url": "https://example.com/app.js", "lineNumber": 10}]}}}}
    text = codec.dumps(frame)
    envelope = None
    large_result_envelope = None
    large_result_full = None
    if codec.FAST_CODEC == "msgspec":
        envelope = _timeit(lambda: codec.decode_envelope(text), iterations)
        # Routing a reply with a large result: the listener only needs the
        # envelope; parsing the 20KB result happens once, in send().
        reply = codec.dumps({"id": 7, "result": {"body": "x" * 20000, "base64Encoded": False}})
        large_result_envelope = _timeit(lambda: codec.decode_envelope(reply), iterations // 4)
        large_result_full = _timeit(lambda: codec.loads(reply), iterations // 4)
    return {
        "codec": codec.FAST_CODEC,
        "encode_frames_per_sec": _timeit(lambda: codec.dumps(frame), iterations),
        "decode_frames_per_sec": _timeit(lambda: codec.loads(text), iterations),
        # Reply routing cost after cycle 24: envelope-only decode. At event
        # frame sizes this is comparable to a full decode; the win is on
        # replies carrying large results, which the listener no longer parses.
        "envelope_decode_frames_per_sec": envelope,
        "large_reply_envelope_per_sec": large_result_envelope,
        "large_reply_full_decode_per_sec": large_result_full,
        "iterations": iterations,
    }


def bench_axtree_tokens(node_count: int = 600) -> Dict[str, Any]:
    nodes = [{"nodeId": "1", "backendDOMNodeId": 1, "role": {"value": "RootWebArea"}, "name": {"value": "Big Page"}, "childIds": [str(i) for i in range(2, node_count)]}]
    for i in range(2, node_count):
        nodes.append({
            "nodeId": str(i), "parentId": "1", "backendDOMNodeId": i,
            "role": {"value": "button" if i % 3 else "link"},
            "name": {"value": f"Action number {i} with a reasonably long label"},
            "childIds": [],
        })
    refs = build_ref_table(nodes)
    view = render_view(refs, title="Big Page", url="http://local/", max_lines=80)
    naive_html = "".join(f"<button>Action number {i} with a reasonably long label</button>" for i in range(node_count))
    return {
        "ax_nodes": node_count,
        "action_refs": len(refs),
        "action_graph_tokens": estimate_tokens(view),
        "naive_dom_tokens_est": estimate_tokens(naive_html),
        "compression_ratio": round(estimate_tokens(naive_html) / max(1, estimate_tokens(view)), 2),
    }


def bench_ring_buffer(iterations: int = 50000) -> Dict[str, Any]:
    buf = EventRingBuffer(capacity=iterations + 10)
    start = time.perf_counter()
    for i in range(iterations):
        buf.put({"seq": i})
    elapsed = time.perf_counter() - start
    return {"events_per_sec": round(iterations / elapsed, 1), "dropped": buf.dropped, "iterations": iterations}


def _process_tree_mb(pid: Optional[int]) -> Optional[float]:
    if not pid:
        return None
    try:
        import psutil

        proc = psutil.Process(pid)
        total = proc.memory_info().rss
        for child in proc.children(recursive=True):
            try:
                total += child.memory_info().rss
            except Exception:
                pass
        return round(total / (1024 * 1024), 1)
    except Exception:
        return None


async def bench_agentbrowser_live(profile: str = "light") -> Dict[str, Any]:
    from agent_browser.agent_api import AgentBrowserController
    from agent_browser.session_registry import find_free_port
    from fixtures.fixture_server import FixtureServer

    with FixtureServer() as srv:
        ctrl = AgentBrowserController(port=find_free_port(), headless=True,
                                      profile="bench_live", launch_profile=profile)
        start = time.perf_counter()
        await ctrl.start(initial_url="about:blank")
        cold_ms = round((time.perf_counter() - start) * 1000, 1)
        try:
            await ctrl.navigate(srv.case_url("http500-null-deref"))
            snap = await ctrl.dom.get_axtree_snapshot()
            view = render_view(snap["refs"], title="bench", url=srv.base_url)
            proc = ctrl.browser_mgr.proc
            return {
                "status": "measured",
                "launch_profile": profile,
                "cold_start_ms": cold_ms,
                "action_graph_tokens": estimate_tokens(view),
                "ax_refs": snap["count"],
                "browser_tree_ram_mb": _process_tree_mb(proc.pid if proc else None),
                "codec": codec.FAST_CODEC,
                "dropped_events": ctrl.cdp.dropped_events if ctrl.cdp else None,
            }
        finally:
            await ctrl.stop()


def bench_playwright_live() -> Dict[str, Any]:
    if not importlib.util.find_spec("playwright"):
        return {"status": "not_installed", "install": "pip install playwright (uses system Chrome via channel='chrome')"}
    try:
        from playwright.sync_api import sync_playwright

        from fixtures.fixture_server import FixtureServer

        with FixtureServer() as srv:
            with sync_playwright() as p:
                start = time.perf_counter()
                browser = p.chromium.launch(channel="chrome", headless=True)
                cold_ms = round((time.perf_counter() - start) * 1000, 1)
                page = browser.new_page()
                page.goto(srv.case_url("http500-null-deref"))
                snapshot_tokens = None
                try:
                    aria = page.locator("body").aria_snapshot()
                    snapshot_tokens = estimate_tokens(aria or "")
                except Exception:
                    snapshot_tokens = estimate_tokens(page.content())
                html_tokens = estimate_tokens(page.content())
                browser.close()
                return {"status": "measured", "cold_start_ms": cold_ms,
                        "snapshot_tokens": snapshot_tokens, "html_tokens": html_tokens}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:300]}


def competitor_status() -> Dict[str, Any]:
    return {
        "playwright": {"installed": bool(importlib.util.find_spec("playwright"))},
        "crawl4ai": {"installed": bool(importlib.util.find_spec("crawl4ai")),
                     "install": "pip install crawl4ai"},
        "puppeteer": {"installed": False, "note": "Node package; not present in this environment (npm ls -g puppeteer empty)."},
    }


def build_report(live: bool = False) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "agentbrowser": {
            "codec": bench_codec(),
            "axtree_tokens": bench_axtree_tokens(),
            "ring_buffer": bench_ring_buffer(),
        },
        "competitors": competitor_status(),
    }
    if live:
        try:
            report["agentbrowser"]["live"] = asyncio.run(bench_agentbrowser_live())
        except Exception as exc:
            report["agentbrowser"]["live"] = {"status": "error", "error": str(exc)[:300]}
        report["competitors"]["playwright_live"] = bench_playwright_live()
    return report


def render_markdown(report: Dict[str, Any]) -> str:
    ab = report["agentbrowser"]
    lines = [
        "# Benchmark results",
        "",
        f"- When: {report['timestamp']}  Platform: {report['platform']}  Python: {report['python']}",
        f"- Codec: {ab['codec']['codec']} encode {ab['codec']['encode_frames_per_sec']}/s, decode {ab['codec']['decode_frames_per_sec']}/s, envelope-route {ab['codec'].get('envelope_decode_frames_per_sec')}/s",
        f"- AXTree: {ab['axtree_tokens']['action_graph_tokens']} action-graph tokens vs ~{ab['axtree_tokens']['naive_dom_tokens_est']} naive DOM tokens ({ab['axtree_tokens']['compression_ratio']}x)",
        f"- Ring buffer: {ab['ring_buffer']['events_per_sec']} events/s, dropped {ab['ring_buffer']['dropped']}",
        "",
        "## Competitors (measured only when actually installed/run)",
        "",
    ]
    for name, info in report["competitors"].items():
        lines.append(f"- {name}: {json.dumps(info)[:400]}")
    live = ab.get("live")
    if live:
        lines += ["", "## AgentBrowser live", "", f"- {json.dumps(live)[:600]}"]
    return "\n".join(lines) + "\n"
