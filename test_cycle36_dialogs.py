"""Cycle 36 tests: dialog policy handling + download capture (fake CDP)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.agent_api import AgentBrowserController


class _FakeCDP:
    def __init__(self):
        self.sent = []

    async def send(self, method, params=None, session_id=None, timeout=15):
        self.sent.append((method, params or {}, session_id))
        return {}


class _Noop:
    async def handle_cdp_event(self, data):
        pass


def _ctrl(policy="dismiss"):
    ctrl = object.__new__(AgentBrowserController)
    ctrl.sessions = {}
    ctrl.active_session_id = "main"
    ctrl.active_target_id = "t"
    ctrl.cdp = _FakeCDP()
    ctrl.network = _Noop()
    ctrl.runtime = _Noop()
    ctrl.user_tracker = _Noop()
    ctrl.source_inspector = _Noop()
    ctrl.dialog_policy = policy
    ctrl.dialog_log = []
    ctrl.downloads = []
    return ctrl


@pytest.mark.asyncio
async def test_dialog_dismissed_by_default_and_logged():
    ctrl = _ctrl()
    await ctrl._on_cdp_event({
        "method": "Page.javascriptDialogOpening", "sessionId": "main",
        "params": {"type": "alert", "message": "hello", "url": "http://x/"},
    })
    assert ctrl.dialog_log[-1]["accepted"] is False
    handled = [p for m, p, _ in ctrl.cdp.sent if m == "Page.handleJavaScriptDialog"]
    assert handled == [{"accept": False}]


@pytest.mark.asyncio
async def test_dialog_accept_policy():
    ctrl = _ctrl(policy="accept")
    await ctrl._on_cdp_event({
        "method": "Page.javascriptDialogOpening", "sessionId": "main",
        "params": {"type": "confirm", "message": "sure?", "url": "http://x/"},
    })
    handled = [p for m, p, _ in ctrl.cdp.sent if m == "Page.handleJavaScriptDialog"]
    assert handled == [{"accept": True}]


@pytest.mark.asyncio
async def test_download_lifecycle_tracked():
    ctrl = _ctrl()
    await ctrl._on_cdp_event({"method": "Browser.downloadWillBegin",
                              "params": {"guid": "g1", "url": "http://x/f.zip", "suggestedFilename": "f.zip"}})
    await ctrl._on_cdp_event({"method": "Browser.downloadProgress",
                              "params": {"guid": "g1", "state": "completed", "receivedBytes": 10, "totalBytes": 10}})
    assert ctrl.downloads[0]["state"] == "completed"
    assert ctrl.downloads[0]["suggested_filename"] == "f.zip"
