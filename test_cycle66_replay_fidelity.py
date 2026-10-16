"""Cycle 66 tests: replay-fidelity metric + hermetic fulfill body stamping."""

import asyncio
import base64
import os
import sys
import urllib.request

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.network import NetworkEngine
from fixtures.fixture_server import FixtureServer
from fixtures.metrics import replay_fidelity
from fixtures.run_replay_fidelity_live import _responses  # noqa: F401  (import guard)


def test_fidelity_metric_exact_partial_and_extra():
    rec = [("/a", 200, "x"), ("/b", 500, "err"), ("/a", 200, "y")]
    assert replay_fidelity(rec, list(rec))["fidelity"] == 1.0
    rep = [("/a", 200, "x"), ("/b", 200, "err"), ("/a", 200, "y")]
    score = replay_fidelity(rec, rep)
    assert score["matched"] == 2 and score["fidelity"] == round(2 / 3, 4)
    assert replay_fidelity(rec, rec[:1])["matched"] == 1
    assert replay_fidelity([], [])["fidelity"] == 1.0


def test_fixture_server_counts_hits():
    with FixtureServer() as srv:
        assert srv.hit_count == 0
        urllib.request.urlopen(srv.base_url + "/index.json", timeout=5).read()
        urllib.request.urlopen(srv.case_url("cors-preflight"), timeout=5).read()
        assert srv.hit_count == 2
        assert srv.hit_paths == ["/index.json", "/case/cors-preflight"]


class _FakeCDP:
    def __init__(self):
        self.sent = []

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.sent.append((method, params))
        return {}


@pytest.mark.asyncio
async def test_fulfill_body_stamped_even_when_pause_precedes_network_event():
    eng = NetworkEngine(_FakeCDP(), lambda: "S1")
    body_b64 = base64.b64encode(b'{"ok":true}').decode()
    eng.hermetic_index = _Index(body_b64)
    # Fetch pause arrives BEFORE Network.requestWillBeSent for this request.
    await eng.handle_cdp_event({"method": "Fetch.requestPaused", "params": {
        "requestId": "interception-job-1.0", "networkId": "N1",
        "request": {"method": "GET", "url": "http://x/api/ping"},
        "resourceType": "XHR"}})
    assert "N1" not in eng.requests  # entry does not exist yet
    await eng.handle_cdp_event({"method": "Network.requestWillBeSent", "params": {
        "requestId": "N1", "request": {"method": "GET", "url": "http://x/api/ping", "headers": {}},
        "type": "XHR", "wallTime": 1.0, "timestamp": 1.0, "initiator": {}}})
    entry = eng.requests["N1"]
    assert entry["response_body"] == '{"ok":true}'
    assert entry["body_source"] == "hermetic_fulfill"


class _Index:
    def __init__(self, body_b64):
        self._body_b64 = body_b64

    def take(self, method, url):
        return {"status": 200, "resp_headers": [], "body_b64": self._body_b64}
