"""Cycle 28 tests: tab listing/activation on the controller."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.agent_api import AgentBrowserController


def _ctrl():
    ctrl = object.__new__(AgentBrowserController)
    ctrl.sessions = {
        "s1": {"type": "page", "targetId": "t1", "url": "http://a/", "title": "A"},
        "s2": {"type": "page", "targetId": "t2", "url": "http://b/", "title": "B"},
        "w1": {"type": "worker", "targetId": "tw", "url": ""},
    }
    ctrl.active_session_id = "s1"
    ctrl.active_target_id = "t1"
    return ctrl


def test_list_tabs_marks_active_and_pages_only():
    tabs = _ctrl().list_tabs()
    assert [t["session_id"] for t in tabs] == ["s1", "s2"]
    assert tabs[0]["active"] is True and tabs[1]["active"] is False


def test_activate_tab_switches_and_validates():
    ctrl = _ctrl()
    out = ctrl.activate_tab("s2")
    assert out["active_session_id"] == "s2"
    assert ctrl.active_session_id == "s2"
    assert ctrl.get_active_session_id() == "s2"
    with pytest.raises(ValueError):
        ctrl.activate_tab("missing")
    with pytest.raises(ValueError):
        ctrl.activate_tab("w1")  # not a page
