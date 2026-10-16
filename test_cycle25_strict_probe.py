"""Cycle 25 tests: strict idle-depth probe in settle verification."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.quiescence import NetworkActivityMonitor
from agent_browser.tracer import ActionTracer


class _Runtime:
    def __init__(self, idle_values):
        self.exceptions = []
        self.console_logs = []
        self.idle_values = list(idle_values)
        self.probes = 0

    async def evaluate(self, expr):
        self.probes += 1
        if "requestIdleCallback" in expr:
            return {"success": True, "value": self.idle_values.pop(0) if self.idle_values else {"idle": True, "rem": 40, "busy": 0}}
        return {"success": True, "value": 1}


class _Network:
    traffic_order = []

    def get_request(self, rid):
        return None


@pytest.mark.asyncio
async def test_strict_probe_rejects_shallow_idle_then_accepts():
    mon = NetworkActivityMonitor()
    # First idle probe is shallow (a timer is due in 2ms); second is deep.
    runtime = _Runtime([{"idle": True, "rem": 2, "busy": 0}, {"idle": True, "rem": 42, "busy": 0}])
    tracer = ActionTracer(None, None, _Network(), runtime, lambda: "sid",
                          activity_monitor=mon, strict_probe=True)
    out = await tracer._settle(0, timeout_s=2.0)
    assert out["settled"] is True
    assert out["probe_verified"] is True
    assert out["idle_depth_ok"] is True
    assert runtime.probes >= 3  # stamp probe + shallow idle + deep idle


@pytest.mark.asyncio
async def test_strict_probe_off_by_default():
    mon = NetworkActivityMonitor()
    runtime = _Runtime([])
    tracer = ActionTracer(None, None, _Network(), runtime, lambda: "sid", activity_monitor=mon)
    out = await tracer._settle(0, timeout_s=1.0)
    assert out["probe_verified"] is True
    assert "idle_depth_ok" not in out
    assert runtime.probes == 1  # only the stamped probe, no idle probe
