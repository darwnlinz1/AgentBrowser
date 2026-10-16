"""Cycle 40 tests: human handoff gate + completion summary."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.agent_api import AgentBrowserController


class _FakeRuntime:
    def __init__(self):
        self.expressions = []

    async def evaluate(self, expr):
        self.expressions.append(expr)
        return {"success": True, "value": True}


class _FakeTracker:
    def __init__(self):
        self.action_history = [{"type": "click", "target": "#a"}]

    def get_actions(self, limit=20, event_type=None):
        return self.action_history[-limit:]


class _FakeTracer:
    async def trace_action(self, action_type, target=None, params=None):
        return {"action": {"success": True}}


def _ctrl():
    ctrl = object.__new__(AgentBrowserController)
    ctrl.awaiting_human = False
    ctrl.handoff_log = []
    ctrl._handoff_started_at = None
    ctrl._handoff_action_baseline = 0
    ctrl._handoff_reason = ""
    ctrl.runtime = _FakeRuntime()
    ctrl.user_tracker = _FakeTracker()
    ctrl.tracer = _FakeTracer()
    ctrl.active_session_id = "s"
    return ctrl


@pytest.mark.asyncio
async def test_handoff_gates_agent_actions_and_summarizes():
    ctrl = _ctrl()
    status = await ctrl.start_handoff(reason="SSO sign-in", instructions="Sign in, then complete")
    assert status["awaiting_human"] is True
    assert any("__agent_handoff_banner" in e for e in ctrl.runtime.expressions)

    with pytest.raises(RuntimeError, match="awaiting human"):
        await ctrl.trace_action("click", target="#go")
    with pytest.raises(RuntimeError, match="awaiting human"):
        await ctrl.navigate("http://x/")
    with pytest.raises(RuntimeError, match="awaiting human"):
        await ctrl.evaluate("1")

    # Human performs two actions while in control.
    ctrl.user_tracker.action_history += [{"type": "click", "target": "#sso"}, {"type": "change", "target": "#otp"}]
    out = await ctrl.complete_handoff(summary="signed in")
    assert out["awaiting_human"] is False
    assert out["completion"]["human_actions_count"] == 2
    assert out["completion"]["summary"] == "signed in"

    # Agent control resumes.
    trace = await ctrl.trace_action("click", target="#go")
    assert trace["action"]["success"] is True
