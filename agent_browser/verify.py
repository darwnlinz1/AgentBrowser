"""
OBSERVE -> INSPECT -> TRACE -> MODIFY -> REPLAY -> DEBUG -> VERIFY gates.

VERIFY passes only when every gate holds. A gate that merely fails to find a
problem is not evidence: each gate needs its positive artifact (a pre-fix
reproduction, a regression test that failed before and passes after, a causal
edge that disappeared, stability across reruns). This prevents the loop from
declaring victory on a coincidence or 'healing' a test by weakening it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def evaluate_verify_gates(
    *,
    reproduced_before: bool,
    regression_failed_before: bool,
    regression_passed_after: bool,
    causal_edge_present_before: bool,
    causal_edge_present_after: bool,
    new_errors_after: int = 0,
    stable_runs: Optional[List[bool]] = None,
    min_stable: int = 3,
) -> Dict[str, Any]:
    stable = list(stable_runs or [])
    gates = {
        "bug_reproduced_before_fix": bool(reproduced_before),
        "regression_test_failed_before": bool(regression_failed_before),
        "regression_test_passed_after": bool(regression_passed_after),
        "causal_edge_removed": bool(causal_edge_present_before and not causal_edge_present_after),
        "no_new_errors": int(new_errors_after) == 0,
        "stable_across_reruns": len(stable) >= min_stable and all(stable),
    }
    passed = all(gates.values())
    return {
        "verified": passed,
        "gates": gates,
        "failed_gates": [name for name, ok in gates.items() if not ok],
        "stable_runs": len(stable),
    }


class DebugLoopState:
    """Explicit state machine for one debugging episode (hypothesis ledger)."""

    STATES = ("OBSERVE", "INSPECT", "TRACE", "MODIFY", "REPLAY", "DEBUG", "VERIFY", "DONE", "BLOCKED")

    def __init__(self):
        self.state = "OBSERVE"
        self.ledger: List[Dict[str, Any]] = []

    def record(self, prediction: str, outcome: Any, evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        entry = {"state": self.state, "prediction": prediction, "outcome": outcome, "evidence": evidence or {}}
        self.ledger.append(entry)
        return entry

    def advance(self, next_state: str) -> str:
        if next_state not in self.STATES:
            raise ValueError(f"unknown state: {next_state}")
        self.state = next_state
        return self.state

    def finish_verify(self, gates_result: Dict[str, Any]) -> str:
        self.record("VERIFY gates all pass", gates_result.get("verified"), gates_result)
        self.state = "DONE" if gates_result.get("verified") else "BLOCKED"
        return self.state
