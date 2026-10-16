"""
Record once, replay deterministically: named flows of agent actions.

A flow step records the action, its target selector, the element fingerprint
at record time, and any text/variable. Replaying runs each step through the
tracer; when a step's selector no longer resolves, the stored fingerprint
heals it to a fresh snapshot ref (fail-closed below the heal threshold).
Secrets are stored as variables (``${VAR}``), never literal values.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class FlowStep:
    action: str  # click | type | navigate
    target: Optional[str] = None
    text: Optional[str] = None
    fingerprint: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"action": self.action, "target": self.target, "text": self.text,
                "fingerprint": self.fingerprint}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FlowStep":
        return cls(action=data["action"], target=data.get("target"),
                   text=data.get("text"), fingerprint=data.get("fingerprint"))


@dataclass
class Flow:
    name: str
    steps: List[FlowStep] = field(default_factory=list)
    start_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "start_url": self.start_url,
                "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Flow":
        return cls(name=data["name"], start_url=data.get("start_url"),
                   steps=[FlowStep.from_dict(s) for s in data.get("steps") or []])

    def save(self, directory: str) -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{self.name}.flow.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)
        return path

    @classmethod
    def load(cls, path: str) -> "Flow":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


def substitute_vars(text: Optional[str], variables: Dict[str, str]) -> Optional[str]:
    """Expand ${VAR} from the supplied mapping; unknown vars are left as-is."""
    if text is None:
        return None

    def _sub(m):
        return variables.get(m.group(1), m.group(0))

    return _VAR.sub(_sub, text)


class FlowRecorder:
    """Collects steps during a live session (agent or human demonstrated)."""

    def __init__(self, name: str, start_url: Optional[str] = None):
        self.flow = Flow(name=name, start_url=start_url)

    def record(self, action: str, target: Optional[str] = None, text: Optional[str] = None,
               fingerprint: Optional[Dict[str, Any]] = None) -> FlowStep:
        step = FlowStep(action=action, target=target, text=text, fingerprint=fingerprint)
        self.flow.steps.append(step)
        return step


async def replay_flow(ctrl: Any, flow: Flow, variables: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Replay a flow through a controller's tracer.

    Each step is traced; a step whose target vanished is healed from its
    fingerprint (via the DOM engine) and retried once by ref. The report
    carries per-step outcomes plus every heal as evidence.
    """
    variables = variables or {}
    results: List[Dict[str, Any]] = []
    if flow.start_url:
        await ctrl.navigate(flow.start_url)
    for index, step in enumerate(flow.steps):
        entry: Dict[str, Any] = {"index": index, "action": step.action, "target": step.target}
        try:
            if step.action == "navigate":
                res = await ctrl.navigate(substitute_vars(step.target, variables) or "")
                entry["ok"] = bool(res.get("navigated", True))
            elif step.action == "type":
                report = await ctrl.tracer.trace_action(
                    "type", target=step.target,
                    params={"text": substitute_vars(step.text, variables) or ""})
                entry["ok"] = bool(report["action"]["success"])
                entry["trace_action"] = report["action"]
            else:  # click and anything tracer-backed
                report = await ctrl.tracer.trace_action(step.action, target=step.target)
                entry["ok"] = bool(report["action"]["success"])
                entry["trace_action"] = report["action"]
        except Exception as exc:
            entry["ok"] = False
            entry["error"] = str(exc)[:200]
            # Heal attempt: fingerprint -> fresh ref, retry once.
            if step.fingerprint and hasattr(ctrl, "dom") and hasattr(ctrl.dom, "resolve_healed_ref"):
                healed = await ctrl.dom.resolve_healed_ref(step.target or "")
                entry["heal"] = healed
                if healed and healed.get("ref"):
                    try:
                        report = await ctrl.tracer.trace_action(step.action, target=f"#{healed['ref']}")
                        entry["ok"] = bool(report["action"]["success"])
                        entry["healed_to_ref"] = healed["ref"]
                    except Exception as exc2:
                        entry["heal_error"] = str(exc2)[:200]
        results.append(entry)
    return {
        "flow": flow.name,
        "steps": results,
        "passed": all(r["ok"] for r in results),
        "heals": [r["heal"] for r in results if r.get("heal")],
    }
