"""Cycle 12 tests: hermetic replay (fulfill-from-recording, no live path)."""

import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.network import NetworkEngine
from agent_browser.replay import ExchangeIndex, exchange_from_entry, stub_for


def _entries():
    e1 = {"id": "1", "method": "GET", "url": "https://app.example/api/items?page=1", "status": 200,
          "response_headers": {"Content-Type": "application/json"}, "response_body": '{"page":1}'}
    e2 = {"id": "2", "method": "GET", "url": "https://app.example/api/items?page=1", "status": 200,
          "response_headers": {"Content-Type": "application/json"}, "response_body": '{"page":2}'}
    return [exchange_from_entry(e1, "x:1"), exchange_from_entry(e2, "x:2")]


def test_exchange_index_is_sequence_aware():
    idx = ExchangeIndex(_entries())
    first = idx.take("GET", "https://app.example/api/items?page=1")
    second = idx.take("GET", "https://app.example/api/items?page=1")
    third = idx.take("GET", "https://app.example/api/items?page=1")
    assert base64.b64decode(first["body_b64"]).decode() == '{"page":1}'
    assert base64.b64decode(second["body_b64"]).decode() == '{"page":2}'
    assert third["uid"] == "x:2"  # exhausted bucket repeats the last (polling)
    assert idx.take("GET", "https://app.example/unknown") is None


def test_exchange_index_keep_set_for_minimization():
    idx = ExchangeIndex(_entries(), keep={"x:2"})
    assert idx.take("GET", "https://app.example/api/items?page=1") is None  # x:1 not kept


class _FakeCDP:
    def __init__(self):
        self.sent = []

    async def send(self, method, params=None, session_id=None, timeout=15):
        self.sent.append((method, params or {}))
        return {}


@pytest.mark.asyncio
async def test_hermetic_handler_never_continues_live():
    cdp = _FakeCDP()
    eng = NetworkEngine(cdp, lambda: "sid")
    eng.requests["r1"] = {"id": "r1", "method": "GET", "url": "https://app.example/api/items?page=1",
                          "status": 200, "response_headers": {"Content-Type": "application/json"},
                          "response_body": '{"page":1}'}
    await eng.enable_hermetic_replay([eng.requests["r1"]])

    # Recorded request -> fulfilled with the recorded body.
    await eng.handle_cdp_event({
        "method": "Fetch.requestPaused", "sessionId": "sid",
        "params": {"requestId": "f1", "requestStage": "Request", "resourceType": "Fetch",
                   "request": {"url": "https://app.example/api/items?page=1", "method": "GET", "headers": {}}},
    })
    # Unrecorded request -> stub, still never continued.
    await eng.handle_cdp_event({
        "method": "Fetch.requestPaused", "sessionId": "sid",
        "params": {"requestId": "f2", "requestStage": "Request", "resourceType": "Script",
                   "request": {"url": "https://cdn.example/lib.js", "method": "GET", "headers": {}}},
    })
    # Even a recorded-shape POST is served from the recording/stub, not sent live.
    await eng.handle_cdp_event({
        "method": "Fetch.requestPaused", "sessionId": "sid",
        "params": {"requestId": "f3", "requestStage": "Request", "resourceType": "Fetch",
                   "request": {"url": "https://app.example/api/submit", "method": "POST", "headers": {}}},
    })
    methods = [m for m, _ in cdp.sent]
    assert "Fetch.continueRequest" not in methods
    fulfills = [p for m, p in cdp.sent if m == "Fetch.fulfillRequest"]
    assert len(fulfills) == 3
    assert base64.b64decode(fulfills[0]["body"]).decode() == '{"page":1}'
    assert stub_for("Image")["status"] == 200
