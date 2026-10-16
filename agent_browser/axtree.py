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


def _ref_line(r: Dict[str, Any]) -> str:
    states = []
    if r.get("disabled"):
        states.append("disabled")
    if r.get("checked") is not None:
        states.append(f"checked={r['checked']}")
    if r.get("expanded") is not None:
        states.append(f"expanded={r['expanded']}")
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
