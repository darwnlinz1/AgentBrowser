"""Cycle 45 tests: DOMSnapshot layout join (rect/paint/viewport/occlusion)."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.axtree import build_ref_table, join_layout, parse_domsnapshot_layout

AX = [
    {"nodeId": "1", "backendDOMNodeId": 10, "role": {"value": "RootWebArea"}, "name": {"value": "Page"}, "childIds": ["2", "3"]},
    {"nodeId": "2", "parentId": "1", "backendDOMNodeId": 11, "role": {"value": "button"}, "name": {"value": "Covered"}, "childIds": []},
    {"nodeId": "3", "parentId": "1", "backendDOMNodeId": 12, "role": {"value": "button"}, "name": {"value": "Below fold"}, "childIds": []},
]

DOCS = [{
    "nodes": {"backendNodeId": [10, 11, 12, 99]},
    "layout": {
        "nodeIndex": [1, 2, 3],
        "bounds": [[10, 10, 100, 30], [10, 5000, 100, 30], [0, 0, 500, 500]],
        "paintOrders": [1, 1, 9],
        "styles": [[], [], []],
    },
}]


def test_parse_and_join_layout():
    refs = build_ref_table(AX)
    layout = parse_domsnapshot_layout(DOCS)
    assert layout[11]["rect"] == {"x": 10, "y": 10, "width": 100, "height": 30}
    joined = join_layout(refs, layout, {"pageX": 0, "pageY": 0, "clientWidth": 800, "clientHeight": 600})
    by_name = {r["name"]: r for r in joined}
    # A higher-paint-order overlay box (backend 99) covers the first button.
    assert by_name["Covered"]["occluded"] is True
    assert by_name["Covered"]["in_viewport"] is True
    assert by_name["Below fold"]["occluded"] is False
    assert by_name["Below fold"]["in_viewport"] is False
