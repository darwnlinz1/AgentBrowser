"""Cycle 55 tests: delta-vs-fresh root-digest correctness invariant."""

import os
import random
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.axtree import (
    apply_ref_diff, build_ref_table, delta_consistent, diff_refs, refs_digest,
)
from agent_browser.dom import DOMEngine


def _node(ax_id, backend, role, name, parent=None, checked=None):
    node = {
        "nodeId": ax_id, "backendDOMNodeId": backend,
        "role": {"value": role}, "name": {"value": name},
        "childIds": [], "ignored": False,
    }
    if parent is not None:
        node["parentId"] = parent
    if checked is not None:
        node["properties"] = [{"name": "checked", "value": {"value": checked}}]
    return node


def _tree(label="Save", checked=False, extra=False, drop=False):
    nodes = [
        _node("root", 1, "RootWebArea", "Page"),
        _node("b1", 10, "button", label, parent="root"),
        _node("c1", 11, "checkbox", "Agree", parent="root", checked=checked),
    ]
    nodes[0]["childIds"] = ["b1", "c1"]
    if extra:
        nodes.append(_node("b2", 12, "button", "Extra", parent="root"))
        nodes[0]["childIds"].append("b2")
    if drop:
        nodes = [n for n in nodes if n["nodeId"] != "c1"]
        nodes[0]["childIds"] = ["b1"]
    return nodes


def test_delta_reconstructs_fresh_state_for_each_mutation_kind():
    base = build_ref_table(_tree())
    variants = [
        build_ref_table(_tree(label="Save now")),       # rename (changed)
        build_ref_table(_tree(checked=True)),           # state toggle (changed)
        build_ref_table(_tree(extra=True)),             # added node
        build_ref_table(_tree(drop=True)),              # removed node
        build_ref_table(_tree()),                       # no change
    ]
    for fresh in variants:
        diff = diff_refs(base, fresh)
        assert delta_consistent(base, diff, fresh) is True
        assert refs_digest(apply_ref_diff(base, diff)) == refs_digest(fresh)


def test_digest_detects_real_change_and_ignores_ref_renumbering():
    a = build_ref_table(_tree())
    b = build_ref_table(_tree(label="Other"))
    assert refs_digest(a) != refs_digest(b)
    renumbered = [dict(r, ref=99 - i) for i, r in enumerate(a)]
    assert refs_digest(renumbered) == refs_digest(a)


def test_random_mutation_sequences_stay_consistent():
    rng = random.Random(20261016)
    current = build_ref_table(_tree())
    labels = ["Save", "Pay", "Go", "Send", "OK"]
    checked = False
    extra = False
    for step in range(60):
        checked = rng.random() < 0.5
        extra = rng.random() < 0.5
        fresh = build_ref_table(_tree(
            label=rng.choice(labels), checked=checked, extra=extra,
            drop=rng.random() < 0.25))
        diff = diff_refs(current, fresh)
        assert delta_consistent(current, diff, fresh) is True, f"step {step}"
        current = fresh


class _FakeCDP:
    def __init__(self):
        self.label = "Save"
        self.sent = []

    async def send(self, method, params=None, session_id=None, timeout=15):
        self.sent.append(method)
        if method == "Accessibility.getFullAXTree":
            return {"nodes": _tree(label=self.label)}
        return {}


@pytest.mark.asyncio
async def test_snapshot_reports_delta_consistency():
    dom = DOMEngine(_FakeCDP(), lambda: "sid")
    first = await dom.get_axtree_snapshot()
    assert first["delta_consistent"] is None  # no previous capture yet
    dom.cdp.label = "Save now"
    second = await dom.get_axtree_snapshot()
    assert second["diff_from_previous"]["changed"]
    assert second["delta_consistent"] is True
