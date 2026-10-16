"""Cycle 2 tests: loaderId/same-document navigation waits. No browser."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.cdp import wait_for_navigation


class _FakeCDP:
    def __init__(self, navigate_result, events=()):
        self.handlers = []
        self.navigate_result = navigate_result
        self.events = list(events)

    def add_event_handler(self, h):
        self.handlers.append(h)

    def remove_event_handler(self, h):
        if h in self.handlers:
            self.handlers.remove(h)

    async def send(self, method, params=None, session_id=None, timeout=15):
        assert method == "Page.navigate"
        # Emit buffered events as Chrome would around the reply.
        for ev in self.events:
            for h in list(self.handlers):
                h(ev)
        return self.navigate_result


@pytest.mark.asyncio
async def test_same_document_navigation_resolves_fast():
    cdp = _FakeCDP(
        {"loaderId": None, "frameId": "f1"},
        events=[{"method": "Page.navigatedWithinDocument", "sessionId": "sid",
                 "params": {"frameId": "f1", "url": "http://x/#/route", "navigationType": "historyApi"}}],
    )
    out = await asyncio.wait_for(wait_for_navigation(cdp, "sid", "http://x/#/route", timeout=5), timeout=2)
    assert out["navigated"] is True
    assert out["same_document"] is True
    assert out["loaded"] is True


@pytest.mark.asyncio
async def test_lifecycle_load_records_loader_id():
    cdp = _FakeCDP(
        {"loaderId": "L9", "frameId": "f1"},
        events=[{"method": "Page.lifecycleEvent", "sessionId": "sid",
                 "params": {"frameId": "f1", "loaderId": "L9", "name": "load", "timestamp": 1.0}}],
    )
    out = await wait_for_navigation(cdp, "sid", "http://x/page", timeout=2)
    assert out["navigated"] is True
    assert out["loaded"] is True
    assert out["lifecycleLoaderId"] == "L9"


class _DynamicCDP(_FakeCDP):
    def __init__(self, navigate_result, pre_events=()):
        super().__init__(navigate_result, events=pre_events)

    def emit(self, event):
        for h in list(self.handlers):
            h(event)


@pytest.mark.asyncio
async def test_stale_lifecycle_load_does_not_resolve():
    # A load from a different loader (stale doc / iframe) during navigate
    # must not satisfy this navigation's wait.
    cdp = _DynamicCDP(
        {"loaderId": "L-new", "frameId": "f1"},
        pre_events=[{"method": "Page.lifecycleEvent", "sessionId": "sid",
                     "params": {"frameId": "f9", "loaderId": "L-stale", "name": "load", "timestamp": 0.5}}],
    )
    task = asyncio.create_task(wait_for_navigation(cdp, "sid", "http://x/page", timeout=3))
    await asyncio.sleep(0.15)
    assert not task.done(), "stale lifecycle load resolved the wait"
    cdp.emit({"method": "Page.lifecycleEvent", "sessionId": "sid",
              "params": {"frameId": "f1", "loaderId": "L-new", "name": "load", "timestamp": 1.0}})
    out = await asyncio.wait_for(task, timeout=2)
    assert out["navigated"] is True
    assert out["loaded"] is True
    assert out["lifecycleLoaderId"] == "L-new"


@pytest.mark.asyncio
async def test_stale_load_event_fired_before_reply_does_not_resolve():
    cdp = _DynamicCDP(
        {"loaderId": "L-new", "frameId": "f1"},
        pre_events=[{"method": "Page.loadEventFired", "sessionId": "sid", "params": {"timestamp": 0.5}}],
    )
    task = asyncio.create_task(wait_for_navigation(cdp, "sid", "http://x/page", timeout=3))
    await asyncio.sleep(0.15)
    assert not task.done(), "pre-reply loadEventFired (previous document) resolved the wait"
    cdp.emit({"method": "Page.loadEventFired", "sessionId": "sid", "params": {"timestamp": 1.0}})
    out = await asyncio.wait_for(task, timeout=2)
    assert out["loaded"] is True


@pytest.mark.asyncio
async def test_navigation_error_text_still_fails():
    cdp = _FakeCDP({"loaderId": "L1", "errorText": "net::ERR_UNSAFE_PORT"})
    out = await wait_for_navigation(cdp, "sid", "http://127.0.0.1:1", timeout=2)
    assert out["navigated"] is False
    assert "ERR_UNSAFE_PORT" in out["error"]
