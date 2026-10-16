"""
Journal-driven minimization over live hermetic replay.

Pipeline: journal events (order + priors) are merged with response bodies
from the network buffer (bodies are not CDP events, so journal-only
exchanges are stub-only), then CDD removes exchanges while a hermetic
replay of the recorded action keeps reproducing the failure signature.
Every candidate run is hermetic -- nothing reaches a live backend.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from agent_browser.evidence import url_template
from agent_browser.minimizer import cdd_async
from agent_browser.replay import build_units, exchange_from_entry


def merge_bodies(journal_exchanges: List[Dict[str, Any]],
                 network_entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach recorded bodies (from the network buffer) to journal exchanges."""
    by_key: Dict[tuple, List[Dict[str, Any]]] = {}
    for entry in network_entries or []:
        ex = exchange_from_entry(entry)
        key = ((ex.get("method") or "GET"), url_template(ex.get("url") or "", keep_query_values=True))
        by_key.setdefault(key, []).append(ex)

    merged = []
    for jex in journal_exchanges or []:
        ex = dict(jex)
        key = ((ex.get("method") or "GET"), url_template(ex.get("url") or "", keep_query_values=True))
        bucket = by_key.get(key)
        if bucket:
            body_ex = bucket.pop(0)
            ex["body_b64"] = body_ex.get("body_b64") or ""
            ex["resp_headers"] = body_ex.get("resp_headers") or []
            if body_ex.get("status") is not None:
                ex["status"] = body_ex.get("status")
            ex["servable"] = bool(body_ex.get("servable", True)) and bool(ex["body_b64"] or ex["status"])
            ex["resource_type"] = body_ex.get("resource_type") or ex.get("resource_type")
        merged.append(ex)
    return merged


async def minimize_exchanges(
    ctrl: Any,
    *,
    exchanges: List[Dict[str, Any]],
    start_url: str,
    action: str,
    target: Optional[str],
    symptom_check: Callable[[Dict[str, Any]], bool],
    target_url: Optional[str] = None,
    trials: int = 1,
) -> Dict[str, Any]:
    """
    CDD-minimize the exchange set while hermetic replay reproduces the bug.

    ``ctrl`` is an AgentBrowserController positioned anywhere; each candidate
    run enables hermetic replay with the candidate keep-set, navigates,
    traces the action, and disables hermetic mode again.
    """
    units = build_units(exchanges, target_url=target_url)
    runs = {"n": 0}

    async def predicate(candidate_units: List[Dict[str, Any]]) -> bool:
        runs["n"] += 1
        keep = {u.get("uid") for u in candidate_units}
        await ctrl.network.enable_hermetic_replay(exchanges, keep=keep)
        try:
            await ctrl.navigate(start_url)
            trace = await ctrl.tracer.trace_action(action, target=target)
            return bool(symptom_check(trace))
        finally:
            await ctrl.network.disable_hermetic_replay()

    minimal_units = await cdd_async(units, predicate, trials=trials)
    keep_uids = {u.get("uid") for u in minimal_units}
    return {
        "minimal_uids": sorted(k for k in keep_uids if k),
        "minimal_urls": sorted({u.get("url") for u in minimal_units if u.get("url")}),
        "original_count": len(units),
        "minimal_count": len(minimal_units),
        "reduction_ratio": round(1 - (len(minimal_units) / len(units)), 4) if units else 0.0,
        "predicate_runs": runs["n"],
    }
