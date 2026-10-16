"""Cycle 11 tests: flow record/persist/replay with heal fallback."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.flows import Flow, FlowRecorder, replay_flow, substitute_vars


def test_flow_roundtrip_and_vars(tmp_path):
    rec = FlowRecorder("checkout-demo", start_url="http://local/case")
    rec.record("click", target="#go")
    rec.record("type", target="#q", text="hello ${USER_NAME}", fingerprint={"role": "textbox", "name": "Search"})
    path = rec.flow.save(str(tmp_path))
    loaded = Flow.load(path)
    assert loaded.name == "checkout-demo"
    assert loaded.steps[1].fingerprint["name"] == "Search"
    assert substitute_vars("hi ${USER_NAME} ${MISSING}", {"USER_NAME": "sam"}) == "hi sam ${MISSING}"


class _FakeTracer:
    def __init__(self, fail_first_for=None):
        self.calls = []
        self.fail_first_for = fail_first_for or set()
        self._failed = set()

    async def trace_action(self, action, target=None, params=None):
        self.calls.append((action, target, params))
        if target in self.fail_first_for and target not in self._failed:
            self._failed.add(target)
            raise ValueError(f"element gone: {target}")
        return {"action": {"success": True, "type": action, "target": target}}


class _FakeDOM:
    async def resolve_healed_ref(self, selector):
        return {"ref": 7, "confidence": 0.9, "evidence": {"kind": "fingerprint_heal"}}


class _FakeCtrl:
    def __init__(self):
        self.tracer = _FakeTracer(fail_first_for={"#buy"})
        self.dom = _FakeDOM()
        self.navigated = []

    async def navigate(self, url):
        self.navigated.append(url)
        return {"navigated": True, "url": url}


@pytest.mark.asyncio
async def test_replay_flow_heals_broken_step():
    ctrl = _FakeCtrl()
    flow = Flow(name="f", start_url="http://local/", steps=[])
    from agent_browser.flows import FlowStep
    flow.steps = [
        FlowStep(action="click", target="#go"),
        FlowStep(action="click", target="#buy", fingerprint={"role": "button", "name": "Buy"}),
        FlowStep(action="type", target="#q", text="x ${V}", fingerprint=None),
    ]
    out = await replay_flow(ctrl, flow, variables={"V": "42"})
    assert ctrl.navigated == ["http://local/"]
    assert out["passed"] is True
    assert out["steps"][1]["healed_to_ref"] == 7
    assert out["heals"], "heal evidence recorded"
    # Variable was substituted before typing.
    assert ctrl.tracer.calls[-1][2]["text"] == "x 42"
