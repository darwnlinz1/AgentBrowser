"""Cycle 14 tests: stamped-probe settle verification."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.quiescence import NetworkActivityMonitor
from agent_browser.tracer import ActionTracer


class _FakeRuntime:
    def __init__(self, monitor=None, bump_on_probe=False):
        self.exceptions = []
        self.console_logs = []
        self.monitor = monitor
        self.bump_on_probe = bump_on_probe
        self.probes = 0

    async def evaluate(self, expr):
        self.probes += 1
        if self.bump_on_probe and self.monitor is not None:
            # Simulate activity landing while the probe was in flight.
            self.monitor.on_event({
                "method": "Network.requestWillBeSent",
                "params": {"requestId": "late-1", "type": "Fetch",
                           "request": {"url": "https://x/late", "method": "GET"}},
            })
            self.bump_on_probe = False  # only once; the re-wait then finishes it
            self.monitor.on_event({"method": "Network.loadingFinished", "params": {"requestId": "late-1"}})
        await asyncio.sleep(0)
        return {"success": True, "value": 1}


class _FakeNetwork:
    traffic_order = []

    def get_request(self, rid):
        return None


@pytest.mark.asyncio
async def test_settle_probe_verified_when_quiet():
    mon = NetworkActivityMonitor()
    tracer = ActionTracer(None, None, _FakeNetwork(), _FakeRuntime(mon), lambda: "sid", activity_monitor=mon)
    out = await tracer._settle(0, timeout_s=1.0)
    assert out["settled"] is True
    assert out["probe_verified"] is True
    assert out["activity_source"] == "sync_monitor"


@pytest.mark.asyncio
async def test_settle_rewaits_when_probe_stamp_changes():
    mon = NetworkActivityMonitor()
    runtime = _FakeRuntime(mon, bump_on_probe=True)
    tracer = ActionTracer(None, None, _FakeNetwork(), runtime, lambda: "sid", activity_monitor=mon)
    out = await tracer._settle(0, timeout_s=2.0)
    # Activity arrived mid-probe: the first quiet reading was discarded and
    # the settle re-waited instead of resolving early on stale evidence.
    assert runtime.probes >= 2
    assert out["settled"] is True
    assert out["probe_verified"] is True
