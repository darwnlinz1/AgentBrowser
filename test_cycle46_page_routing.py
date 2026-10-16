"""Cycle 46 tests: popup release after session setup + CLI --page surface."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.agent_api import AgentBrowserController


class _FakeCDP:
    def __init__(self):
        self.sent = []

    async def send(self, method, params=None, session_id=None, timeout=15):
        self.sent.append((method, session_id))
        return {}


class _FakeEngine:
    def __init__(self):
        self.enabled_on = []

    async def enable(self, session_id=None):
        self.enabled_on.append(session_id)


class _FakeNetwork(_FakeEngine):
    async def enable_network(self, session_id=None, **kw):
        self.enabled_on.append(session_id)


@pytest.mark.asyncio
async def test_page_session_released_after_enable():
    ctrl = object.__new__(AgentBrowserController)
    ctrl.cdp = _FakeCDP()
    ctrl.network = _FakeNetwork()
    ctrl.runtime = _FakeEngine()
    ctrl.user_tracker = _FakeEngine()
    ctrl.source_inspector = _FakeEngine()
    await ctrl._enable_page_session("tab-2")
    methods = [m for m, _ in ctrl.cdp.sent]
    assert "Runtime.runIfWaitingForDebugger" in methods
    # Release happens after the domain enables, not before setup.
    assert methods.index("Runtime.runIfWaitingForDebugger") > methods.index("Page.enable")
    assert ctrl.network.enabled_on == ["tab-2"]


def test_cli_exposes_page_option():
    out = os.popen(f'"{sys.executable}" -m agent_browser --help').read()
    assert "--page" in out
