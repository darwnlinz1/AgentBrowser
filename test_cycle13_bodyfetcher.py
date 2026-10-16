"""Cycle 13 tests: body fetches run off the event hot path, bounded."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.network import NetworkEngine


class _SlowBodyCDP:
    def __init__(self):
        self.release = asyncio.Event()
        self.calls = []

    async def send(self, method, params=None, session_id=None, timeout=15):
        self.calls.append(method)
        if method == "Network.getResponseBody":
            await self.release.wait()
            return {"body": '{"ok":true}', "base64Encoded": False}
        return {}


@pytest.mark.asyncio
async def test_loading_finished_does_not_block_on_body_fetch():
    cdp = _SlowBodyCDP()
    eng = NetworkEngine(cdp, lambda: "sid")
    await eng.handle_cdp_event({
        "method": "Network.requestWillBeSent", "sessionId": "sid",
        "params": {"requestId": "r1", "request": {"url": "https://x/api", "method": "GET", "headers": {}},
                   "wallTime": 1, "timestamp": 1, "type": "Fetch", "initiator": {}},
    })
    # The handler must return promptly even though getResponseBody is stuck.
    await asyncio.wait_for(eng.handle_cdp_event({
        "method": "Network.loadingFinished", "sessionId": "sid",
        "params": {"requestId": "r1", "timestamp": 1.1, "encodedDataLength": 10},
    }), timeout=1.0)
    entry = eng.get_request("r1")
    assert entry["response_body"] is None  # still pending, handler not blocked
    assert eng.body_fetch_stats["submitted"] == 1

    cdp.release.set()
    entry = await eng.await_body("r1", timeout=2.0)
    assert entry["response_body"] == '{"ok":true}'
    assert eng.body_fetch_stats["completed"] == 1


@pytest.mark.asyncio
async def test_body_fetch_failure_is_recorded_from_pool():
    class _FailCDP:
        async def send(self, method, params=None, session_id=None, timeout=15):
            raise RuntimeError("No resource with given identifier found")

    eng = NetworkEngine(_FailCDP(), lambda: "sid")
    await eng.handle_cdp_event({
        "method": "Network.requestWillBeSent", "sessionId": "sid",
        "params": {"requestId": "r2", "request": {"url": "https://x/a", "method": "GET", "headers": {}},
                   "wallTime": 1, "timestamp": 1, "type": "XHR", "initiator": {}},
    })
    await eng.handle_cdp_event({
        "method": "Network.loadingFinished", "sessionId": "sid",
        "params": {"requestId": "r2", "timestamp": 1.2, "encodedDataLength": 5},
    })
    await asyncio.sleep(0.1)
    entry = eng.get_request("r2")
    assert entry["body_error"]
    assert "getResponseBody_failed" in entry["capture_gaps"]
