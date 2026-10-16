"""
Phase 3 tests: in-browser replay shapes, HAR export, response-stage
interception (fake CDP), intervention promotion, ddmin, VERIFY gates.
No browser is launched.
"""

import asyncio
import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.intervention import choose_intervention, evaluate_intervention
from agent_browser.minimizer import ddmin, minimize_journal
from agent_browser.network import InterceptRule, NetworkEngine
from agent_browser.verify import DebugLoopState, evaluate_verify_gates


class _FakeCDP:
    def __init__(self, evaluate_value=None, response_body="orig-body"):
        self.sent = []
        self.evaluate_value = evaluate_value or {}
        self.response_body = response_body

    async def send(self, method, params=None, session_id=None, timeout=15):
        self.sent.append((method, params or {}))
        if method == "Runtime.evaluate":
            return {"result": {"value": self.evaluate_value}}
        if method == "Fetch.getResponseBody":
            return {"body": base64.b64encode(self.response_body.encode()).decode(), "base64Encoded": True}
        return {}


def _engine_with_entry(cdp=None):
    eng = NetworkEngine(cdp or _FakeCDP(), lambda: "sid")
    eng.requests["r1"] = {
        "id": "r1", "url": "https://api.example/data", "method": "GET",
        "headers": {"Accept": "application/json", "Cookie": "sid=secret"},
        "request_body": "", "status": 200, "response_body": '{"v":1}',
        "response_headers": {"Content-Type": "application/json"}, "mime_type": "application/json",
        "timestamp": 1700000000, "duration_ms": 12,
    }
    eng.traffic_order = ["r1"]
    return eng


@pytest.mark.asyncio
async def test_replay_in_page_fetch_diff_and_shape():
    cdp = _FakeCDP(evaluate_value={"ok": True, "status": 200, "headers": {}, "body": '{"v":2}'})
    eng = _engine_with_entry(cdp)
    out = await eng.replay_in_page_fetch("r1")
    assert out["success"] is True
    assert out["replay_kind"] == "in_page_fetch"
    assert out["replayed"]["status"] == 200
    assert '"v":2' in out["diff"] or "v" in out["diff"]
    # Cookie must not be forwarded as a manual header (browser attaches its own).
    sent_expr = cdp.sent[0][1]["expression"]
    assert "sid=secret" not in sent_expr


def test_har_export_redacted_by_default():
    eng = _engine_with_entry()
    har = eng.export_har()
    entry = har["log"]["entries"][0]
    assert entry["request"]["url"] == "https://api.example/data"
    hdr = {h["name"]: h["value"] for h in entry["request"]["headers"]}
    assert hdr["Cookie"] == "[redacted]"
    assert eng.export_har(include_secrets=True)["log"]["entries"][0]["request"]["headers"][1]["value"] == "sid=secret"


@pytest.mark.asyncio
async def test_response_stage_match_and_replace():
    cdp = _FakeCDP(response_body="orig-body")
    eng = NetworkEngine(cdp, lambda: "sid")
    eng.add_rule(InterceptRule(rule_id="rw", url_pattern=r"/api/data", action="fulfill",
                               modify_response_body="replaced-body"))
    await eng.handle_cdp_event({
        "method": "Fetch.requestPaused", "sessionId": "sid",
        "params": {
            "requestId": "f1", "requestStage": "Response", "resourceType": "Fetch",
            "request": {"url": "https://x/api/data", "method": "GET", "headers": {}},
            "responseStatusCode": 200,
            "responseHeaders": [{"name": "Content-Type", "value": "application/json"}],
        },
    })
    methods = [m for m, _ in cdp.sent]
    assert "Fetch.getResponseBody" in methods
    fulfill = [p for m, p in cdp.sent if m == "Fetch.fulfillRequest"][0]
    assert base64.b64decode(fulfill["body"]).decode() == "replaced-body"
    assert eng.intercept_rules["rw"].response_hits == 1


@pytest.mark.asyncio
async def test_enable_interception_response_stage_opt_in():
    cdp = _FakeCDP()
    eng = NetworkEngine(cdp, lambda: "sid")
    await eng.enable_interception()
    assert cdp.sent[-1][1]["patterns"] == [{"urlPattern": "*", "requestStage": "Request"}]
    await eng.enable_interception(include_response=True)
    stages = {p["requestStage"] for p in cdp.sent[-1][1]["patterns"]}
    assert stages == {"Request", "Response"}


@pytest.mark.asyncio
async def test_response_continue_fallback_preserves_original_body():
    class _CDPFallback(_FakeCDP):
        async def send(self, method, params=None, session_id=None, timeout=15):
            if method == "Fetch.continueResponse":
                self.sent.append((method, params or {}))
                raise RuntimeError("continueResponse unavailable")
            return await super().send(method, params, session_id, timeout)

    cdp = _CDPFallback(response_body="must-not-be-stripped")
    eng = NetworkEngine(cdp, lambda: "sid")
    await eng.handle_cdp_event({
        "method": "Fetch.requestPaused", "sessionId": "sid",
        "params": {
            "requestId": "f2", "requestStage": "Response", "resourceType": "Fetch",
            "request": {"url": "https://x/other", "method": "GET", "headers": {}},
            "responseStatusCode": 200,
            "responseHeaders": [{"name": "Content-Type", "value": "text/plain"}],
        },
    })
    fulfill = [p for m, p in cdp.sent if m == "Fetch.fulfillRequest"]
    assert fulfill, "expected a fulfill fallback"
    assert base64.b64decode(fulfill[0]["body"]).decode() == "must-not-be-stripped"


def test_write_replay_refused_without_allow_write():
    eng = _engine_with_entry()
    eng.requests["w1"] = {**eng.requests["r1"], "id": "w1", "method": "POST", "request_body": "{}"}
    out = eng.replay_request("w1")
    assert out["success"] is False
    assert "allow_write" in out["error"]
    # Safe methods still replay (mocked at the requests layer would need network;
    # here we only assert the gate does not block GET before the HTTP call by
    # pointing it at an unroutable loopback URL and expecting a network error
    # dict, not the write-gate error).
    eng.requests["r1"]["url"] = "http://127.0.0.1:9/data"
    out_get = eng.replay_request("r1")
    assert "allow_write" not in str(out_get.get("error", ""))


def test_ai_summary_redacts_body_secrets():
    eng = _engine_with_entry()
    eng.requests["r1"]["request_body"] = '{"user":"sam","password":"hunter2","access_token":"tok-abc"}'
    eng.requests["r1"]["response_body"] = '{"session_token":"resp-secret-1","ok":true}'
    summary = eng.format_ai_summary()
    assert "hunter2" not in summary
    assert "tok-abc" not in summary
    assert "resp-secret-1" not in summary
    assert "[redacted]" in summary
    assert "sam" in summary  # non-secret fields survive


def test_intervention_promotion_requires_all_control_none_treatment():
    assert evaluate_intervention([True, True], [False, False])["promoted"] is True
    assert evaluate_intervention([True, False], [False, False])["promoted"] is False
    assert evaluate_intervention([True, True], [True, False])["promoted"] is False
    assert evaluate_intervention([True], [False])["promoted"] is False  # not enough runs
    hyp = {"kind": "single_failure_null_consumer", "evidence": {"url": "https://api.example/fail"}}
    assert choose_intervention(hyp)["type"] == "fulfill_recorded_good_response"


def test_ddmin_isolates_minimal_repro():
    steps = [{"a": 1}, {"noise": 1}, {"trigger": True}, {"noise": 2}, {"noise": 3}]

    def repro(candidate):
        return any(s.get("trigger") for s in candidate)

    assert ddmin(steps, repro) == [{"trigger": True}]
    out = minimize_journal(steps, repro, trials=1)
    assert out["minimal_count"] == 1 and out["reduction_ratio"] == 0.8


def test_ddmin_flaky_majority():
    calls = {"n": 0}

    def flaky(candidate):
        # Reproduces only on alternating calls; majority of 3 still detects it.
        calls["n"] += 1
        has = any(s.get("trigger") for s in candidate)
        return has and (calls["n"] % 2 == 1 or calls["n"] % 3 == 0)

    result = ddmin([{"trigger": True}, {"x": 1}], flaky, trials=3)
    assert {"trigger": True} in result


def test_verify_gates_all_required():
    good = evaluate_verify_gates(
        reproduced_before=True, regression_failed_before=True, regression_passed_after=True,
        causal_edge_present_before=True, causal_edge_present_after=False,
        new_errors_after=0, stable_runs=[True, True, True],
    )
    assert good["verified"] is True
    bad = evaluate_verify_gates(
        reproduced_before=True, regression_failed_before=True, regression_passed_after=True,
        causal_edge_present_before=True, causal_edge_present_after=True,
        new_errors_after=0, stable_runs=[True, True, True],
    )
    assert bad["verified"] is False and "causal_edge_removed" in bad["failed_gates"]

    loop = DebugLoopState()
    loop.advance("TRACE")
    assert loop.finish_verify(good) == "DONE"
    assert loop.finish_verify(bad) == "BLOCKED"
