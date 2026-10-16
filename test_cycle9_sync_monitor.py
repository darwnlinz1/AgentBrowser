"""Cycle 9 tests: sync CDP subscribers + NetworkActivityMonitor."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.cdp import CDPConnection
from agent_browser.quiescence import NetworkActivityMonitor


def _ev(method, **params):
    return {"method": method, "params": params}


def test_monitor_ledger_excludes_long_lived():
    mon = NetworkActivityMonitor()
    mon.on_event(_ev("Network.requestWillBeSent", requestId="r1", type="Fetch",
                     request={"url": "https://x/api", "method": "GET"}))
    mon.on_event(_ev("Network.requestWillBeSent", requestId="ws1", type="EventSource",
                     request={"url": "https://x/events", "method": "GET"}))
    assert mon.in_flight == {"r1"}
    assert mon.long_lived == {"ws1"}
    mon.on_event(_ev("Network.loadingFinished", requestId="r1"))
    assert mon.in_flight == set()
    snap = mon.snapshot()
    assert snap["activity_seq"] >= 2


@pytest.mark.asyncio
async def test_sync_handlers_survive_async_task_cap():
    conn = CDPConnection("ws://127.0.0.1:9/x")
    conn._max_handler_tasks = 0  # every async handler would be shed
    seen = []
    conn.add_sync_handler(lambda data: seen.append(data.get("method")))

    async def _handler(data):
        seen.append("async:" + str(data.get("method")))

    conn.add_event_handler(_handler)

    class _WS:
        def __init__(self):
            self._msgs = [
                '{"method": "Network.requestWillBeSent", "params": {}}',
                '{"method": "Runtime.exceptionThrown", "params": {}}',
            ]

        async def recv(self):
            if self._msgs:
                return self._msgs.pop(0)
            await asyncio.sleep(60)

        async def close(self):
            pass

    conn.ws = _WS()
    conn.running = True
    task = asyncio.create_task(conn._listen_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    # Sync subscriber saw both events even with the async cap at zero.
    assert seen == ["Network.requestWillBeSent", "Runtime.exceptionThrown"]
    assert conn.dropped_events == 2
    assert conn.sync_handler_errors == 0
