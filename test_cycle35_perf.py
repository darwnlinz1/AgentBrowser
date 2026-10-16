"""Cycle 35 tests: web-vitals grading + PerfEngine wiring (fake CDP)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.perf import PerfEngine, grade_vitals


def test_grade_vitals_bands():
    assert grade_vitals({"lcp_ms": 1800, "cls": 0.05, "long_task_count": 1, "max_long_task_ms": 120}) == {
        "lcp": "good", "cls": "good", "long_tasks": "good"}
    g = grade_vitals({"lcp_ms": 5200, "cls": 0.4, "long_task_count": 2, "max_long_task_ms": 900})
    assert g == {"lcp": "poor", "cls": "poor", "long_tasks": "poor"}
    assert grade_vitals({})["lcp"] == "unknown"


class _FakeCDP:
    async def send(self, method, params=None, session_id=None, timeout=15):
        if method == "Runtime.evaluate":
            return {"result": {"value": {
                "url": "http://x/", "navigation": {"load": 300},
                "lcp_ms": 900, "lcp_element": "h1", "cls": 0.02,
                "long_tasks": [], "long_task_count": 0, "max_long_task_ms": 0,
            }}}
        if method == "Performance.getMetrics":
            return {"metrics": [{"name": "Nodes", "value": 120}, {"name": "JSHeapUsedSize", "value": 2048}]}
        return {}


@pytest.mark.asyncio
async def test_perf_snapshot_shape():
    eng = PerfEngine(_FakeCDP(), lambda: "sid")
    snap = await eng.snapshot()
    assert snap["vitals"]["grades"]["lcp"] == "good"
    assert snap["vitals"]["lcp_element"] == "h1"
    assert snap["renderer"]["Nodes"] == 120
