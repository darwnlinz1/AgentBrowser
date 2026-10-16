"""Cycle 47 tests: scoped queryAXTree re-reads with diffs."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.dom import DOMEngine
from agent_browser.focus import FocusEngine


def _subtree(text):
    return [
        {"nodeId": "r", "backendDOMNodeId": 50, "role": {"value": "group"}, "name": {"value": "Cart"}, "childIds": ["b"]},
        {"nodeId": "b", "parentId": "r", "backendDOMNodeId": 51, "role": {"value": "button"}, "name": {"value": text}, "childIds": []},
    ]


class _FakeCDP:
    def __init__(self):
        self.label = "Checkout"
        self.sent = []

    async def send(self, method, params=None, session_id=None, timeout=15):
        self.sent.append(method)
        if method == "Runtime.evaluate":
            return {"result": {"objectId": "obj-1"}}
        if method == "DOM.describeNode":
            return {"node": {"backendNodeId": 50}}
        if method == "Accessibility.queryAXTree":
            assert params.get("backendNodeId") == 50
            return {"nodes": _subtree(self.label)}
        return {}


@pytest.mark.asyncio
async def test_scoped_axtree_and_diff():
    dom = DOMEngine(_FakeCDP(), lambda: "sid")
    first = await dom.get_scoped_axtree(selector="#cart")
    assert first["count"] == 1
    assert first["refs"][0]["name"] == "Checkout"
    assert first["diff_from_previous"] is None

    dom.cdp.label = "Pay now"
    second = await dom.get_scoped_axtree(selector="#cart")
    diff = second["diff_from_previous"]
    assert len(diff["changed"]) == 1
    assert diff["changed"][0]["after"]["name"] == "Pay now"


@pytest.mark.asyncio
async def test_focus_scoped_view_uses_dom():
    dom = DOMEngine(_FakeCDP(), lambda: "sid")
    focus = FocusEngine(dom.cdp, lambda: "sid", dom_engine=dom)
    focus.pin("#cart", alias="cart")
    view = await focus.get_scoped_focused_view()
    assert view["pinned_count"] == 1
    assert view["regions"]["cart"]["refs"][0]["name"] == "Checkout"
