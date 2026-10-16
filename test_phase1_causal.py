"""
Phase 1 tests: stack-stamped attribution, quiescence, Audits, signature oracle.
No browser is launched.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.diagnostics import DiagnosticEngine
from agent_browser.evidence import (
    build_trace_report,
    initiator_chain,
    make_action_tag,
    stack_contains_marker,
    summarize_request,
)
from agent_browser.quiescence import QuiescenceTracker, is_long_lived, wait_for_quiescence
from agent_browser.runtime import RuntimeEngine


def _initiator_with_parent(tag_url=None, leaf_url="https://app.example/app.js"):
    stack = {
        "callFrames": [{"url": leaf_url, "lineNumber": 3, "columnNumber": 1, "functionName": "onClick"}],
        "parent": {
            "callFrames": [{"url": tag_url or "https://app.example/framework.js", "lineNumber": 9, "columnNumber": 2, "functionName": "dispatch"}],
            "parent": {"callFrames": [], "description": "async-task"},
        },
    }
    return {"type": "script", "stack": stack}


def test_stack_chain_walking_and_marker():
    tag = make_action_tag("123-click")
    stack = _initiator_with_parent(tag_url=f"http://x/{tag}.js")["stack"]
    assert stack_contains_marker(stack, tag)
    assert not stack_contains_marker(stack, make_action_tag("other"))
    chain = initiator_chain(_initiator_with_parent())
    assert chain[0]["function"] == "onClick"
    assert len(chain) == 2  # sync frame + parent frame


def test_attributed_request_counts_background_stays_temporal():
    tag = make_action_tag("42-click")
    tagged = {
        "id": "api", "url": "https://api.example/failing-endpoint", "method": "GET", "status": 500,
        "resource_type": "Fetch", "initiator": _initiator_with_parent(tag_url=f"http://x/{tag}.js"),
    }
    background = {
        "id": "ping", "url": "https://app.example/api/background-ping", "method": "GET", "status": 200,
        "resource_type": "Fetch", "initiator": _initiator_with_parent(),
    }
    report = build_trace_report(
        action_type="click", target="#go", params={}, action_error=None,
        action_result={"clicked": True}, start_time=1.0, duration_ms=100,
        pre_url="http://t/", post_url="http://t/", post_title="t",
        mutations=[], mutation_truncated=False,
        observed_requests=[
            summarize_request(tagged, "window", action_tag=tag),
            summarize_request(background, "window", action_tag=tag),
        ],
        observed_exceptions=[{
            "description": "TypeError: Cannot read properties of undefined (reading 'id')",
            "url": "http://t/", "text": "TypeError",
        }],
        observed_console_errors=[], window_note="test", action_tag=tag,
        settle_info={"settled": True, "settle_reason": "quiescent"},
    )
    by_id = {r["id"]: r for r in report["network"]["requests"]}
    assert by_id["api"]["confidence"] == "attributed"
    assert by_id["api"]["caused_by_action"] is True
    assert by_id["ping"]["confidence"] == "temporal"
    assert report["network"]["requests_triggered_count"] == 1
    kinds = {(l["kind"], l["confidence"]) for l in report["evidence"]["causal_links"]}
    assert ("stack_attribution", "attributed") in kinds
    assert report["evidence"]["settle"]["settle_reason"] == "quiescent"


def test_fresh_tracker_is_not_quiet_immediately():
    import time as _time
    tr = QuiescenceTracker(quiet_window_s=0.25)
    assert not tr.is_quiet()  # would false-early on debounced effects
    assert tr.is_quiet(now=_time.monotonic() + 1.0)


def test_action_floor_blocks_early_quiet():
    tr = QuiescenceTracker(quiet_window_s=0.1, floor_s=0.35)
    tr.last_activity = 999.0  # synthetic clock base for this test
    assert not tr.is_quiet(now=1000.0)   # floor anchored here, +0.35s
    assert not tr.is_quiet(now=1000.2)   # still inside the floor
    assert tr.is_quiet(now=1001.0)       # floor passed and quiet window met


def test_quiescence_long_lived_never_blocks():
    assert is_long_lived("WebSocket", "wss://x/socket")
    assert is_long_lived("EventSource", "https://x/events")
    assert not is_long_lived("Fetch", "https://x/api/data")
    tr = QuiescenceTracker(quiet_window_s=0.1)
    tr.note_request_start("ws1", "WebSocket", "wss://x/socket", now=100.0)
    tr.note_request_start("r1", "Fetch", "https://x/api", now=100.0)
    assert not tr.is_quiet(now=100.05)
    tr.note_request_end("r1", now=100.1)
    assert tr.is_quiet(now=100.25)  # ws1 still open but ignored


@pytest.mark.asyncio
async def test_wait_for_quiescence_timeout_is_deadlock_proof():
    tr = QuiescenceTracker(quiet_window_s=0.1)
    tr.note_request_start("stuck", "Fetch", "https://x/slow")
    info = await wait_for_quiescence(tr, timeout_s=0.15)
    assert info["settled"] is False
    assert info["settle_reason"] == "timeout"
    assert info["residual_requests"] == ["stuck"]


@pytest.mark.asyncio
async def test_audits_issue_added_is_structured():
    engine = RuntimeEngine(None, lambda: "sid")
    await engine.handle_cdp_event({
        "method": "Audits.issueAdded",
        "params": {"issue": {
            "code": "CorsIssue",
            "details": {"corsIssueDetails": {
                "request": {"requestId": "r9", "url": "https://api.example/data"},
                "corsErrorStatus": {"corsError": "MissingAllowOriginHeader"},
            }},
        }},
    })
    issues = engine.get_audit_issues()
    assert issues[-1]["category"] == "CORS_POLICY"
    assert issues[-1]["request_id"] == "r9"


def test_failure_signature_and_regression_test():
    engine = DiagnosticEngine(None, None, None, None, None)
    sig = DiagnosticEngine.failure_signature(
        [{"description": "TypeError: Cannot read properties of undefined (reading 'id') at line 42"}],
        [{"url": "https://api.example/orders/123?x=1", "status": 500}],
    )
    assert sig["exception_class"] == "TypeError"
    assert sig["failing_request"]["status"] == 500
    assert "{n}" in sig["failing_request"]["path"]

    async def _gen():
        return await engine.generate_regression_test(
            "http://127.0.0.1:9/case/http500-null-deref",
            {"type": "click", "target": "#go"},
            signature=sig,
        )

    script = asyncio.run(_gen())
    compile(script, "<regression>", "exec")
    assert 'SELECTOR = "#go"' in script
    assert "EXPECTED_FAIL_STATUS = 500" in script
    assert "failure signature not reproduced" in script
