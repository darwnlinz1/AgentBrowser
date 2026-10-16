"""
OBSERVE -> INSPECT -> TRACE -> MODIFY -> REPLAY -> DEBUG -> VERIFY orchestrator.

This drives one debugging episode on a live controller and refuses to call it
verified unless the evidence gates in ``verify.py`` all hold. The MODIFY step
is a caller-supplied fix hook (for example an interception rule, or a real
code change followed by a reload); the loop itself never edits the target
application and never weakens an assertion to make a gate pass.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from agent_browser.diagnostics import DiagnosticEngine
from agent_browser.evidence import url_template
from agent_browser.verify import DebugLoopState, evaluate_verify_gates


def trace_signature(trace: Dict[str, Any]) -> Dict[str, Any]:
    """Failure signature of one trace report (diagnostics' normalized oracle)."""
    runtime = trace.get("runtime") or {}
    network = trace.get("network") or {}
    return DiagnosticEngine.failure_signature(
        runtime.get("exceptions") or [], network.get("requests") or [])


def symptom_matches(trace: Dict[str, Any], signature: Dict[str, Any]) -> bool:
    """
    True when a trace reproduces the signature: the failing request
    (path template + status) AND the exception class both appear.
    A signature with no failing request falls back to exception-only, and
    vice versa; an empty signature never matches.
    """
    runtime = trace.get("runtime") or {}
    network = trace.get("network") or {}
    failing = signature.get("failing_request") or {}
    exc_class = signature.get("exception_class")

    fail_ok = True
    if failing:
        fail_ok = any(
            isinstance(r.get("status"), int)
            and r.get("status") == failing.get("status")
            and url_template(r.get("url")) == failing.get("path")
            for r in network.get("requests") or []
        )
    exc_ok = True
    if exc_class:
        exc_ok = any(
            exc_class in str(e.get("description") or e.get("text") or "")
            for e in runtime.get("exceptions") or []
        )
    if not failing and not exc_class:
        return False
    return bool(fail_ok and exc_ok)


def _causal_edge_present(trace: Dict[str, Any]) -> bool:
    """
    The symptom's causal edge: a *failing* request tied to the action.
    A successful attributed request after a fix is not the edge -- the edge
    is gone precisely because the request no longer fails.
    """
    network = trace.get("network") or {}
    requests = network.get("requests") or []
    failing = [
        r for r in requests
        if r.get("status") == "FAILED" or (isinstance(r.get("status"), int) and r.get("status") >= 400)
    ]
    if not failing:
        return False
    if any(r.get("confidence") in ("supported", "attributed", "interventional") for r in failing):
        return True
    failing_ids = {r.get("id") for r in failing}
    evidence = trace.get("evidence") or {}
    return any(
        link.get("confidence") in ("supported", "attributed", "interventional")
        and (link.get("evidence") or {}).get("request_id") in failing_ids
        for link in evidence.get("causal_links") or []
    )


ALLOWED_ACTIONS = ("click", "type", "navigate", "eval", "hover", "scroll")
MAX_VERIFY_RUNS = 10


class DebugLoop:
    """One evidence-gated debugging episode over an AgentBrowserController."""

    def __init__(self, controller: Any):
        self.ctrl = controller
        self.state = DebugLoopState()

    async def run_episode(
        self,
        target_url: str,
        *,
        action: str = "click",
        target: Optional[str] = None,
        fix_hook: Optional[Callable[[Any], Awaitable[None]]] = None,
        clear_hook: Optional[Callable[[Any], Awaitable[None]]] = None,
        verify_runs: int = 3,
        regression_runner: Optional[Callable[[str], Awaitable[bool]]] = None,
    ) -> Dict[str, Any]:
        """
        Run the episode. ``fix_hook(ctrl)`` applies the candidate fix between
        the before/after traces; ``regression_runner(phase)`` optionally runs
        an external regression check and returns True when the bug reproduces.
        Without a fix hook, VERIFY honestly reports the un-fixed state.
        """
        if action not in ALLOWED_ACTIONS:
            raise ValueError(
                f"unsupported debug action {action!r}; expected one of {ALLOWED_ACTIONS}")
        # Clamp reruns so API/MCP callers cannot turn one episode into an
        # unbounded trace loop against the daemon.
        verify_runs = min(MAX_VERIFY_RUNS, max(1, int(verify_runs)))
        started = time.perf_counter()
        loop = self.state

        # OBSERVE
        await self.ctrl.navigate(target_url)
        view = await self.ctrl.get_agent_view()
        loop.record("page loads and the action target is observable",
                    {"url": target_url, "view_chars": len(view or "")})

        # INSPECT (baseline health before the action)
        loop.advance("INSPECT")
        baseline = await self.ctrl.diagnostics.run_diagnostics()
        loop.record("baseline diagnostics captured", baseline.get("health"),
                    {"total_issues": baseline.get("total_issues")})

        # TRACE (reproduce)
        loop.advance("TRACE")
        trace_before = await self.ctrl.tracer.trace_action(action, target=target)
        signature = trace_signature(trace_before)
        reproduced_before = symptom_matches(trace_before, signature)
        causal_before = _causal_edge_present(trace_before)
        time_to_root_cause_ms = round((time.perf_counter() - started) * 1000, 1)
        loop.record("action reproduces the failure signature", reproduced_before,
                    {"signature": signature, "causal_edge": causal_before,
                     "time_to_root_cause_ms": time_to_root_cause_ms})

        regression_before: Optional[bool] = None
        if regression_runner is not None:
            regression_before = bool(await regression_runner("before"))

        # DEBUG (ranked, evidence-graded hypotheses on current state)
        loop.advance("DEBUG")
        diagnostics = await self.ctrl.diagnostics.run_diagnostics()
        loop.record("diagnostics produce ranked hypotheses",
                    [h.get("kind") for h in diagnostics.get("hypotheses") or []],
                    {"health": diagnostics.get("health")})

        # MODIFY + REPLAY (apply the candidate fix, then re-trace)
        loop.advance("MODIFY")
        if fix_hook is not None:
            await fix_hook(self.ctrl)
            loop.record("candidate fix applied via fix_hook", True)
        else:
            loop.record("no fix hook supplied; after-state is the un-fixed page", False)

        loop.advance("REPLAY")
        after_traces: List[Dict[str, Any]] = []
        try:
            for _ in range(max(1, verify_runs)):
                after_traces.append(await self.ctrl.tracer.trace_action(action, target=target))
        finally:
            if clear_hook is not None:
                await clear_hook(self.ctrl)

        symptom_after = [symptom_matches(t, signature) for t in after_traces]
        causal_after = any(_causal_edge_present(t) for t in after_traces)
        stable = [not s for s in symptom_after]
        final_exceptions = (after_traces[-1].get("runtime") or {}).get("exceptions") or [] if after_traces else []
        new_errors = 0
        for exc in final_exceptions:
            desc = str(exc.get("description") or exc.get("text") or "")
            if signature.get("exception_class") and signature["exception_class"] in desc:
                continue
            new_errors += 1

        regression_after: Optional[bool] = None
        if regression_runner is not None:
            regression_after = bool(await regression_runner("after"))

        # VERIFY (all gates, positive artifacts only)
        loop.advance("VERIFY")
        gates = evaluate_verify_gates(
            reproduced_before=reproduced_before,
            regression_failed_before=(
                regression_before if regression_runner is not None else reproduced_before),
            regression_passed_after=(
                (not regression_after) if regression_runner is not None else (not any(symptom_after))),
            causal_edge_present_before=causal_before,
            causal_edge_present_after=causal_after,
            new_errors_after=new_errors,
            stable_runs=stable,
            min_stable=max(1, verify_runs),
        )
        final_state = loop.finish_verify(gates)
        return {
            "state": final_state,
            "verified": gates["verified"],
            "gates": gates,
            "signature": signature,
            "time_to_root_cause_ms": time_to_root_cause_ms,
            "baseline_health": baseline.get("health"),
            "diagnostics_health": diagnostics.get("health"),
            "hypotheses": diagnostics.get("hypotheses"),
            "verify_runs": verify_runs,
            "regression_source": "external_runner" if regression_runner is not None else "trace_oracle",
            "symptom_after_runs": symptom_after,
            "ledger": loop.ledger,
        }
