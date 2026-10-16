"""Cycle 39 tests: stale AX ref revalidation and unique re-anchoring."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.axtree import build_ref_table, find_reanchor, ref_freshness, summarize_partial
from agent_browser.dom import DOMEngine


def _node(ax_id, backend, name, role="button"):
    return {
        "nodeId": ax_id,
        "backendDOMNodeId": backend,
        "role": {"value": role},
        "name": {"value": name},
        "childIds": [],
        "properties": [],
    }


ORIGINAL = [_node("1", 10, "Shop", "RootWebArea"), _node("2", 11, "Buy now")]
FRESH_ONE = [_node("1", 20, "Shop", "RootWebArea"), _node("2", 21, "Buy now")]
FRESH_TWO = FRESH_ONE + [_node("3", 22, "Buy now")]


def test_ref_freshness_identity_vs_state():
    stored = build_ref_table(ORIGINAL)[0]
    live = summarize_partial([_node("2", 11, "Buy later")])
    out = ref_freshness(stored, live)
    assert out["status"] == "stale"
    assert out["identity_changed"] is True

    changed = dict(stored)
    changed_live = summarize_partial([{**_node("2", 11, "Buy now"), "properties": [{"name": "disabled", "value": {"value": True}}]}])
    out2 = ref_freshness(stored, changed_live)
    assert out2["status"] == "changed"
    assert out2["identity_changed"] is False


def test_find_reanchor_requires_unique_identity():
    stored = build_ref_table(ORIGINAL)[0]
    assert find_reanchor(stored, build_ref_table(FRESH_ONE))["candidate"]["backend_id"] == 21
    assert find_reanchor(stored, build_ref_table(FRESH_TWO))["candidate"] is None
    assert find_reanchor(stored, build_ref_table(FRESH_TWO))["candidate_count"] == 2


class _FakeCDP:
    def __init__(self, fresh_nodes):
        self.fresh_nodes = fresh_nodes
        self.sent = []

    async def send(self, method, params=None, session_id=None, timeout=15):
        params = params or {}
        self.sent.append((method, params))
        if method == "Accessibility.enable":
            return {}
        if method == "Accessibility.getFullAXTree":
            return {"nodes": self.fresh_nodes}
        if method == "Accessibility.getPartialAXTree":
            if params.get("backendNodeId") == 11:
                return {"nodes": [_node("old", 11, "Buy later")]}
            return {"nodes": [_node("new", params.get("backendNodeId"), "Buy now")]}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": f"obj-{params.get('backendNodeId')}"}}
        if method == "Runtime.callFunctionOn":
            fn = params.get("functionDeclaration", "")
            if "dispatchEvent" in fn:
                return {"result": {"value": {"clicked": True, "x": 5, "y": 6, "tag": "button", "text": "Buy now"}}}
            return {"result": {"value": {"tag": "button", "text": "Buy now", "rect": {"x": 1, "y": 2, "width": 10, "height": 10}}}}
        if method == "Runtime.evaluate":
            return {"result": {"value": {"t": "Shop", "u": "http://x/"}}}
        return {}


@pytest.mark.asyncio
async def test_resolve_reanchors_unique_stale_ref():
    dom = DOMEngine(_FakeCDP(FRESH_ONE), lambda: "sid")
    dom._ax_refs = build_ref_table(ORIGINAL)
    out = await dom.resolve_ax_ref(1)
    assert out["resolved"] is True
    assert out["backend_id"] == 21
    assert out["freshness"]["status"] == "stale"
    assert out["reanchored"]["from_backend_id"] == 11
    assert out["reanchored"]["to_backend_id"] == 21


class _GoneCDP(_FakeCDP):
    async def send(self, method, params=None, session_id=None, timeout=15):
        params = params or {}
        if method == "Accessibility.getPartialAXTree":
            return {"nodes": []}
        if method == "DOM.resolveNode" and params.get("backendNodeId") == 11:
            return {"object": {}}
        return await super().send(method, params, session_id, timeout)


@pytest.mark.asyncio
async def test_resolve_reanchors_remounted_node():
    dom = DOMEngine(_GoneCDP(FRESH_ONE), lambda: "sid")
    dom._ax_refs = build_ref_table(ORIGINAL)
    out = await dom.resolve_ax_ref(1)
    assert out["resolved"] is True
    assert out["backend_id"] == 21
    assert out["reanchored"]["reason"] == "node_gone"


@pytest.mark.asyncio
async def test_click_fails_closed_when_reanchor_ambiguous():
    dom = DOMEngine(_FakeCDP(FRESH_TWO), lambda: "sid")
    dom._ax_refs = build_ref_table(ORIGINAL)
    with pytest.raises(ValueError, match="could not be clicked"):
        await dom.click_ax_ref(1)
