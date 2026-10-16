"""Cycle 8 tests: child target sessions (OOPIF/worker) get capture enabled."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.agent_api import AgentBrowserController
from agent_browser.network import NetworkEngine
from agent_browser.runtime import RuntimeEngine


class _FakeCDP:
    def __init__(self):
        self.sent = []

    async def send(self, method, params=None, session_id=None, timeout=15):
        self.sent.append((method, session_id))
        return {}


@pytest.mark.asyncio
async def test_engines_enable_on_explicit_child_session():
    cdp = _FakeCDP()
    net = NetworkEngine(cdp, lambda: "main-session")
    await net.enable_network(session_id="child-1")
    assert ("Network.enable", "child-1") in cdp.sent

    rt = RuntimeEngine(cdp, lambda: "main-session")
    await rt.enable(session_id="child-1")
    assert ("Runtime.enable", "child-1") in cdp.sent
    # Default still targets the active session.
    await rt.enable()
    assert ("Runtime.enable", "main-session") in cdp.sent


class _NoopEngine:
    async def handle_cdp_event(self, data):
        pass


@pytest.mark.asyncio
async def test_second_page_gets_full_enable_not_child_enable():
    ctrl = object.__new__(AgentBrowserController)
    ctrl.sessions = {}
    ctrl.active_session_id = "main-1"
    ctrl.active_target_id = "t-main"
    ctrl.cdp = _FakeCDP()
    ctrl.network = _NoopEngine()
    ctrl.runtime = _NoopEngine()
    ctrl.user_tracker = _NoopEngine()
    ctrl.source_inspector = _NoopEngine()

    full, child = [], []

    async def fake_full(sid):
        full.append(sid)

    async def fake_child(sid):
        child.append(sid)

    ctrl._enable_page_session = fake_full
    ctrl._enable_child_session = fake_child

    await ctrl._on_cdp_event({
        "method": "Target.attachedToTarget",
        "params": {"sessionId": "tab-2", "targetInfo": {"type": "page", "targetId": "t2"}},
    })
    await asyncio.sleep(0)
    assert full == ["tab-2"]
    assert child == []
    assert ctrl.active_session_id == "main-1"  # second tab does not steal active


@pytest.mark.asyncio
async def test_attach_enables_child_but_not_second_page_as_active():
    ctrl = object.__new__(AgentBrowserController)
    ctrl.sessions = {}
    ctrl.active_session_id = "main-1"
    ctrl.active_target_id = "t-main"
    ctrl.cdp = _FakeCDP()
    ctrl.network = _NoopEngine()
    ctrl.runtime = _NoopEngine()
    ctrl.user_tracker = _NoopEngine()
    ctrl.source_inspector = _NoopEngine()

    enabled = []

    async def fake_child(sid):
        enabled.append(sid)

    ctrl._enable_child_session = fake_child

    await ctrl._on_cdp_event({
        "method": "Target.attachedToTarget",
        "params": {"sessionId": "oopif-1", "targetInfo": {"type": "iframe", "targetId": "f1"}},
    })
    await asyncio.sleep(0)  # let the fire-and-forget task run
    assert enabled == ["oopif-1"]
    assert ctrl.active_session_id == "main-1"  # child never steals the active page
    assert ctrl.sessions["oopif-1"]["type"] == "iframe"
