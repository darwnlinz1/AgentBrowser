"""
Interventional (counterfactual) confirmation.

Correlation becomes causation only after an experiment: control runs must
reproduce the symptom and treatment runs (one intervention applied) must make
it disappear, k-of-k. This is the top rung of the evidence ladder:
temporal -> weak -> supported -> attributed -> interventional.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

CONFIDENCE_INTERVENTIONAL = "interventional"


def evaluate_intervention(
    control_outcomes: List[bool],
    treatment_outcomes: List[bool],
    *,
    min_runs: int = 2,
) -> Dict[str, Any]:
    """
    ``True`` in an outcome list means 'the symptom reproduced'.

    Promote only when every control reproduced AND no treatment reproduced,
    with at least ``min_runs`` on each side. Anything else stays a hypothesis.
    """
    control = list(control_outcomes or [])
    treatment = list(treatment_outcomes or [])
    enough = len(control) >= min_runs and len(treatment) >= min_runs
    control_all = bool(control) and all(control)
    treatment_none = bool(treatment) and not any(treatment)
    promoted = bool(enough and control_all and treatment_none)
    return {
        "promoted": promoted,
        "confidence": CONFIDENCE_INTERVENTIONAL if promoted else None,
        "control_runs": len(control),
        "treatment_runs": len(treatment),
        "control_reproduced": sum(1 for x in control if x),
        "treatment_reproduced": sum(1 for x in treatment if x),
        "decision": (
            "interventional: symptom present in all controls, absent in all treatments"
            if promoted else
            "not promoted: needs all-control-reproduce + zero-treatment-reproduce with enough runs"
        ),
    }


def choose_intervention(hypothesis: Dict[str, Any]) -> Dict[str, Any]:
    """Cheapest discriminating intervention for a diagnostic hypothesis."""
    kind = (hypothesis or {}).get("kind") or ""
    ev = (hypothesis or {}).get("evidence") or {}
    url = ev.get("url") or ""
    if kind in ("single_failure_null_consumer", "url_overlap"):
        return {
            "type": "fulfill_recorded_good_response",
            "url_pattern": url,
            "rationale": "If the exception disappears when the failing request is fulfilled with a good response, the request causes the exception.",
        }
    return {
        "type": "fail_request",
        "url_pattern": url,
        "rationale": "If the symptom appears when this request is forced to fail, it is on the causal path.",
    }


async def run_intervention_experiment(
    trace_once: Callable[[], Any],
    apply_intervention: Callable[[], Any],
    clear_intervention: Callable[[], Any],
    *,
    symptom_check: Callable[[Dict[str, Any]], bool],
    runs: int = 2,
) -> Dict[str, Any]:
    """
    Drive a control/treatment experiment against a live controller.

    ``trace_once`` performs the action and returns a trace report;
    ``symptom_check(report) -> bool`` decides whether the symptom reproduced.
    Interventions are applied only through the caller's actuators (normally
    the existing InterceptRule machinery) and always cleared afterwards.
    """
    control: List[bool] = []
    for _ in range(runs):
        report = await trace_once()
        control.append(bool(symptom_check(report)))
    treatment: List[bool] = []
    await apply_intervention()
    try:
        for _ in range(runs):
            report = await trace_once()
            treatment.append(bool(symptom_check(report)))
    finally:
        await clear_intervention()
    result = evaluate_intervention(control, treatment, min_runs=runs)
    result["control_outcomes"] = control
    result["treatment_outcomes"] = treatment
    return result
