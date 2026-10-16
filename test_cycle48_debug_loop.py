"""Cycle 48 tests: OBSERVE..VERIFY orchestration with evidence gates."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.loop_engine import DebugLoop, symptom_matches, trace_signature


def _trace(buggy: bool):
    if buggy:
        return {
            "action": {"success": True},
            "network": {"requests": [{
                "url": "https://api.example/failing-endpoint", "status": 500,
                "confidence": "attributed",
            }], "requests_triggered_count": 1, "failing_requests_count": 1},
            "runtime": {"exceptions": [{
                "description": "TypeError: Cannot read properties of undefined (reading 'id')",
                "text": "TypeError",
            }], "exceptions_count": 1},
            "evidence": {"causal_links": [{"confidence": "attributed", "kind": "stack_attribution"}]},
        }
    return {
        "action": {"success": True},
        "network": {"requests": [{
            "url": "https://api.example/failing-endpoint", "status": 200, "confidence": "attributed",
        }], "requests_triggered_count": 1, "failing_requests_count": 0},
        "runtime": {"exceptions": [], "exceptions_count": 0},
        "evidence": {"causal_links": [{"confidence": "attributed", "kind": "stack_attribution"}]},
    }


class _FakeDiagnostics:
    async def run_diagnostics(self):
        return {"health": "UNHEALTHY", "total_issues": 2,
                "hypotheses": [{"kind": "single_failure_null_consumer", "confidence": "weak"}]}


class _FakeTracer:
    def __init__(self, ctrl):
        self.ctrl = ctrl

    async def trace_action(self, action, target=None, params=None):
        return _trace(not self.ctrl.fixed)


class _FakeCtrl:
    def __init__(self):
        self.fixed = False
        self.diagnostics = _FakeDiagnostics()
        self.tracer = _FakeTracer(self)
        self.navigated = []

    async def navigate(self, url):
        self.navigated.append(url)
        return {"navigated": True}

    async def get_agent_view(self):
        return "Page: fixture"


def test_signature_and_symptom_matching():
    sig = trace_signature(_trace(True))
    assert sig["exception_class"] == "TypeError"
    assert sig["failing_request"]["status"] == 500
    assert symptom_matches(_trace(True), sig) is True
    assert symptom_matches(_trace(False), sig) is False
    assert symptom_matches(_trace(True), {}) is False


@pytest.mark.asyncio
async def test_episode_verifies_only_with_a_real_fix():
    ctrl = _FakeCtrl()

    async def fix(c):
        c.fixed = True

    report = await DebugLoop(ctrl).run_episode(
        "http://local/case", target="#go", fix_hook=fix, verify_runs=3)
    assert report["verified"] is True
    assert report["state"] == "DONE"
    assert report["symptom_after_runs"] == [False, False, False]
    assert report["time_to_root_cause_ms"] >= 0
    states = [entry["state"] for entry in report["ledger"]]
    assert states[0] == "OBSERVE" and "VERIFY" in states


@pytest.mark.asyncio
async def test_episode_rejects_unknown_action_and_clamps_verify_runs():
    ctrl = _FakeCtrl()
    with pytest.raises(ValueError, match="unsupported debug action"):
        await DebugLoop(ctrl).run_episode("http://local/case", action="teleport")
    assert ctrl.navigated == []  # rejected before any page work

    async def fix(c):
        c.fixed = True

    report = await DebugLoop(ctrl).run_episode(
        "http://local/case", target="#go", fix_hook=fix, verify_runs=999)
    assert report["verify_runs"] == 10
    assert len(report["symptom_after_runs"]) == 10


@pytest.mark.asyncio
async def test_episode_without_fix_stays_blocked():
    ctrl = _FakeCtrl()
    report = await DebugLoop(ctrl).run_episode("http://local/case", target="#go", verify_runs=2)
    assert report["verified"] is False
    assert report["state"] == "BLOCKED"
    assert "causal_edge_removed" in report["gates"]["failed_gates"]
