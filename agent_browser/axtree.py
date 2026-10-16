"""
Accessibility-tree ref table + Merkle subtree hashing (pure, Chrome-free).

Refs bind to ``backendDOMNodeId`` (node identity), never to attributes injected
into the page. A hierarchical Merkle hash over role/name/value/state lets the
agent receive only the changed subtree after an action instead of a full DOM.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

INTERESTING_ROLES = {
    "button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox",
    "listbox", "option", "tab", "menuitem", "menuitemcheckbox", "switch",
    "slider", "spinbutton", "heading", "cell", "rowheader", "columnheader",
    "treeitem", "navigation", "main", "banner", "contentinfo", "form",
    "dialog", "alertdialog", "progressbar",
}

_TEXT_ROLES = {"heading", "statictext", "text"}


def _prop(node: Dict[str, Any], name: str) -> Any:
    for p in node.get("properties") or []:
        if p.get("name") == name:
            v = p.get("value") or {}
            return v.get("value")
    return None


def node_summary(node: Dict[str, Any]) -> Dict[str, Any]:
    role = ((node.get("role") or {}).get("value") or "").lower()
    name = ((node.get("name") or {}).get("value") or "").strip()
    value = ((node.get("value") or {}).get("value"))
    return {
        "backend_id": node.get("backendDOMNodeId"),
        "ax_id": node.get("nodeId"),
        "role": role,
        "name": name[:120],
        "value": (str(value)[:80] if value is not None else None),
        "disabled": bool(_prop(node, "disabled")),
        "checked": _prop(node, "checked"),
        "expanded": _prop(node, "expanded"),
        "selected": _prop(node, "selected"),
        "child_ids": list(node.get("childIds") or []),
        "parent_id": node.get("parentId"),
    }


def fingerprint(summary: Dict[str, Any]) -> str:
    """Stable content fingerprint; cosmetic layout is deliberately excluded."""
    payload = {
        "r": summary.get("role"), "n": summary.get("name"), "v": summary.get("value"),
        "d": summary.get("disabled"), "c": summary.get("checked"),
        "e": summary.get("expanded"), "s": summary.get("selected"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


_IDENTITY_FIELDS = ("role", "name")
_CHANGE_FIELDS = ("role", "name", "value", "disabled", "checked", "expanded", "selected")


def summarize_partial(nodes: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Summarize the first node of an Accessibility.getPartialAXTree result."""
    if not nodes:
        return None
    return node_summary(nodes[0])


def ref_freshness(stored: Dict[str, Any], live: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compare a stored AX ref against its current live summary.

    Role/name are identity: if either changed, the ref is stale and must not
    be acted on blindly. Value/state changes are reported as ``changed`` --
    visible, but not by themselves proof that node identity moved.
    """
    if not live:
        return {"status": "unknown", "identity_changed": False, "changes": {}}
    changes = {
        field: {"from": stored.get(field), "to": live.get(field)}
        for field in _CHANGE_FIELDS
        if stored.get(field) != live.get(field)
    }
    identity_changed = any(field in changes for field in _IDENTITY_FIELDS)
    return {
        "status": "stale" if identity_changed else ("changed" if changes else "fresh"),
        "identity_changed": identity_changed,
        "changes": changes,
        "live_fingerprint": fingerprint(live),
    }


def find_reanchor(stored: Dict[str, Any], refs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Find a unique fresh ref with the stored identity (same role and name).

    Zero or multiple matches fail closed; a guess between identical buttons
    is exactly how stale refs click the wrong element.
    """
    candidates = [
        ref for ref in refs
        if ref.get("role") == stored.get("role") and ref.get("name") == stored.get("name")
    ]
    return {
        "candidate": candidates[0] if len(candidates) == 1 else None,
        "candidate_count": len(candidates),
        "candidates": candidates[:5],
    }


def parse_domsnapshot_layout(documents: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """
    Map backendNodeId -> layout facts from a DOMSnapshot.captureSnapshot
    result: bounds (document coords), paint order, and a style whitelist.
    Mirrors browser-use's join of DOMSnapshot layout onto DOM nodes.
    """
    out: Dict[int, Dict[str, Any]] = {}
    for doc in documents or []:
        nodes = doc.get("nodes") or {}
        layout = doc.get("layout") or {}
        backend_ids = nodes.get("backendNodeId") or []
        bounds_list = layout.get("bounds") or []
        paint_list = layout.get("paintOrders") or []
        styles_list = layout.get("styles") or []
        for layout_index, node_index in enumerate(layout.get("nodeIndex") or []):
            if node_index is None or node_index >= len(backend_ids):
                continue
            backend_id = backend_ids[node_index]
            bounds = bounds_list[layout_index] if layout_index < len(bounds_list) else []
            rect = None
            if len(bounds) >= 4:
                rect = {"x": bounds[0], "y": bounds[1], "width": bounds[2], "height": bounds[3]}
            out[backend_id] = {
                "rect": rect,
                "paint_order": paint_list[layout_index] if layout_index < len(paint_list) else None,
                "styles": styles_list[layout_index] if layout_index < len(styles_list) else [],
            }
    return out


def join_layout(refs: List[Dict[str, Any]], layout_by_backend: Dict[int, Dict[str, Any]],
                viewport: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Attach rect/paint/viewport facts to AX refs and flag likely occlusion:
    a ref whose center is covered by a higher-paint-order box from the full
    layout set is marked occluded (its click point would hit something else).
    """
    boxes = [
        (info.get("paint_order") or 0, info.get("rect"))
        for info in layout_by_backend.values() if info.get("rect")
    ]
    vp = viewport or {}
    vx, vy = vp.get("pageX", 0), vp.get("pageY", 0)
    vw, vh = vp.get("clientWidth"), vp.get("clientHeight")
    for ref in refs:
        info = layout_by_backend.get(ref.get("backend_id")) or {}
        rect = info.get("rect")
        ref["rect"] = rect
        ref["paint_order"] = info.get("paint_order")
        if rect and vw and vh:
            ref["in_viewport"] = not (
                rect["x"] + rect["width"] < vx or rect["x"] > vx + vw
                or rect["y"] + rect["height"] < vy or rect["y"] > vy + vh
            )
        else:
            ref["in_viewport"] = None
        ref["occluded"] = False
        if rect:
            cx, cy = rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2
            own = info.get("paint_order") or 0
            for paint, other in boxes:
                if paint > own and other["x"] <= cx <= other["x"] + other["width"] \
                        and other["y"] <= cy <= other["y"] + other["height"]:
                    ref["occluded"] = True
                    break
    return refs


def build_ref_table(ax_nodes: List[Dict[str, Any]], include_text: bool = False) -> List[Dict[str, Any]]:
    """Assign [#ref] numbers to actionable/named AX nodes, keyed by backend id."""
    refs: List[Dict[str, Any]] = []
    ref = 1
    for node in ax_nodes or []:
        if node.get("ignored"):
            continue
        s = node_summary(node)
        role = s["role"]
        actionable = role in INTERESTING_ROLES
        textual = include_text and role in _TEXT_ROLES and bool(s["name"])
        if not (actionable or textual):
            continue
        if s["backend_id"] is None:
            continue
        s["ref"] = ref
        s["fingerprint"] = fingerprint(s)
        refs.append(s)
        ref += 1
    return refs


def merkle_hashes(ax_nodes: List[Dict[str, Any]]) -> Dict[Any, str]:
    """
    Per-node Merkle hash: leaf content + ordered child hashes. Changing a deep
    descendant changes every ancestor hash, so a diff can prune unchanged
    subtrees by comparing a single hash per node.
    """
    by_id: Dict[Any, Dict[str, Any]] = {}
    for node in ax_nodes or []:
        s = node_summary(node)
        if s["ax_id"] is not None:
            by_id[s["ax_id"]] = s
    memo: Dict[Any, str] = {}
    visiting: set = set()

    def _hash(ax_id: Any) -> str:
        if ax_id in memo:
            return memo[ax_id]
        if ax_id in visiting:  # cycle guard
            return "cycle"
        visiting.add(ax_id)
        s = by_id.get(ax_id) or {}
        child_hashes = [_hash(c) for c in (s.get("child_ids") or []) if c in by_id]
        payload = fingerprint(s) + "|" + ",".join(child_hashes)
        h = hashlib.sha256(payload.encode()).hexdigest()[:20]
        visiting.discard(ax_id)
        memo[ax_id] = h
        return h

    for ax_id in by_id:
        _hash(ax_id)
    return memo


def diff_refs(old_refs: List[Dict[str, Any]], new_refs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Changed-subtree diff keyed by backendDOMNodeId (+added/-removed/~changed)."""
    old = {r["backend_id"]: r for r in old_refs}
    new = {r["backend_id"]: r for r in new_refs}
    added = [new[k] for k in new.keys() - old.keys()]
    removed = [old[k] for k in old.keys() - new.keys()]
    changed = []
    for k in new.keys() & old.keys():
        if new[k].get("fingerprint") != old[k].get("fingerprint"):
            changed.append({"before": old[k], "after": new[k]})
    return {"added": added, "removed": removed, "changed": changed,
            "unchanged_count": len((new.keys() & old.keys())) - len(changed)}


def refs_digest(refs: List[Dict[str, Any]]) -> str:
    """
    Canonical digest of ref-table state: (backend_id, fingerprint) pairs.

    Ref numbers are presentation, not state, so they are excluded; two
    captures with the same digest hold the same actionable content.
    """
    payload = sorted(
        (str(r.get("backend_id")), r.get("fingerprint") or "") for r in refs or [])
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:20]


def apply_ref_diff(old_refs: List[Dict[str, Any]], diff: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Reconstruct the new ref state from old refs + a ``diff_refs`` delta."""
    by_id: Dict[Any, Dict[str, Any]] = {r["backend_id"]: dict(r) for r in old_refs or []}
    for r in diff.get("removed") or []:
        by_id.pop(r.get("backend_id"), None)
    for change in diff.get("changed") or []:
        after = change.get("after") or {}
        if after.get("backend_id") is not None:
            by_id[after["backend_id"]] = dict(after)
    for r in diff.get("added") or []:
        if r.get("backend_id") is not None:
            by_id[r["backend_id"]] = dict(r)
    return [by_id[k] for k in sorted(by_id, key=lambda k: str(k))]


def delta_consistent(old_refs: List[Dict[str, Any]], diff: Dict[str, Any],
                     new_refs: List[Dict[str, Any]]) -> bool:
    """
    Delta-vs-fresh invariant: applying the delta to the old refs must
    reproduce exactly the fresh capture's digest. If this ever fails, the
    diff is lying and callers must fall back to the full keyframe.
    """
    return refs_digest(apply_ref_diff(old_refs, diff)) == refs_digest(new_refs)


def _ref_line(r: Dict[str, Any]) -> str:
    states = []
    if r.get("disabled"):
        states.append("disabled")
    if r.get("checked") is not None:
        states.append(f"checked={r['checked']}")
    if r.get("expanded") is not None:
        states.append(f"expanded={r['expanded']}")
    if r.get("occluded"):
        states.append("occluded")
    if r.get("in_viewport") is False:
        states.append("offscreen")
    suffix = f" [{' '.join(states)}]" if states else ""
    val = f" value={r['value']!r}" if r.get("value") else ""
    return f"[#{r['ref']}] {r['role']} {r['name']!r}{val}{suffix}"


def render_view(refs: List[Dict[str, Any]], title: str = "", url: str = "", max_lines: int = 120) -> str:
    """Token-lean action graph: one line per actionable node (300-500 tok target)."""
    lines = [f"Page: {title} | URL: {url}", "-" * 50]
    for r in refs[:max_lines]:
        lines.append(_ref_line(r))
    if len(refs) > max_lines:
        lines.append(f"... {len(refs) - max_lines} more actionable nodes")
    return "\n".join(lines)


def render_budgeted_view(
    refs: List[Dict[str, Any]],
    title: str = "",
    url: str = "",
    budget_tokens: int = 400,
) -> str:
    """
    Keyframe view with a hard token budget.

    An explicit ``view`` always returns a full (budgeted) keyframe -- deltas
    are only ever a supplement between keyframes, never the sole state the
    model holds (the design Playwright's withdrawn delta snapshots lacked).
    Lines are admitted in tree order until the budget is exhausted; the
    remainder is summarized in one fold line.
    """
    header = [f"Page: {title} | URL: {url}", "-" * 50]
    used = estimate_tokens("\n".join(header))
    lines = list(header)
    shown = 0
    for r in refs:
        line = _ref_line(r)
        cost = estimate_tokens(line) + 1
        if used + cost > budget_tokens and shown:
            break
        lines.append(line)
        used += cost
        shown += 1
    if shown < len(refs):
        lines.append(f"... {len(refs) - shown} more actionable nodes (budget {budget_tokens} tokens; raise budget or narrow scope)")
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for budget checks in tests."""
    return max(1, len(text) // 4)
