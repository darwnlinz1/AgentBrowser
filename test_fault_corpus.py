"""
Chrome-free tests for the seeded fault corpus: completeness, ground-truth
shape, ephemeral-port serving, endpoint behaviour, and scoring helpers.
No browser is launched.
"""

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from fixtures.fault_corpus import CASES, CASES_BY_ID, all_case_ids, get_case
from fixtures.fixture_server import FixtureServer
from fixtures.metrics import attribution_scores, dropped_event_rate, false_supported_rate

EXPECTED_IDS = {
    "cors-preflight",
    "two-fetch-race",
    "debounced-input",
    "http500-null-deref",
    "iframe-worker-error",
    "hydration-mismatch",
    "broken-redirect-chain",
    "websocket-disconnect",
    "cache-stale-headers",
    "memory-leak-navigation",
}


def _get(url, follow=True):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect) if not follow else urllib.request.build_opener()
    try:
        with opener.open(url, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode("utf-8", "replace")


def test_corpus_has_exactly_the_10_archetypes():
    assert set(all_case_ids()) == EXPECTED_IDS
    assert len(CASES) == 10


def test_every_case_has_ground_truth_and_trigger():
    for case in CASES:
        gt = case["ground_truth"]
        assert gt["action"]["type"] in ("click", "type")
        assert gt["action"]["target"].startswith("#")
        assert gt["expected_link"] in ("none", "temporal", "weak", "supported")
        assert case["html"].startswith("<!DOCTYPE html>")
        # The action target must exist in the served page.
        target_id = gt["action"]["target"][1:]
        assert f'id="{target_id}"' in case["html"] or f"id='{target_id}'" in case["html"]


def test_canonical_case_ground_truth():
    gt = get_case("http500-null-deref")["ground_truth"]
    assert gt["triggered_request"] == "/api/failing-endpoint"
    assert gt["background_requests"] == ["/api/background-ping"]
    assert gt["failing_status"] == 500
    assert "Cannot read properties of undefined" in gt["exception_signature"]


def test_fixture_server_ephemeral_port_and_endpoints():
    with FixtureServer() as srv:
        assert srv.port > 0
        status, _, body = _get(srv.base_url + "/")
        assert status == 200
        assert json.loads(body)["count"] == 10

        status, _, body = _get(srv.case_url("http500-null-deref"))
        assert status == 200 and "Trigger Broken Flow" in body

        status, _, body = _get(srv.base_url + "/api/failing-endpoint")
        assert status == 500 and "Internal Error" in body

        status, _, body = _get(srv.base_url + "/api/cached")
        assert status == 200 and "immutable" in _get(srv.base_url + "/api/cached")[1].get("Cache-Control", "")


def test_redirect_chain_is_preserved_stepwise():
    with FixtureServer() as srv:
        status, headers, _ = _get(srv.base_url + "/api/redirect-start", follow=False)
        assert status == 302 and headers.get("Location") == "/api/redirect-mid"
        status, headers, _ = _get(srv.base_url + "/api/redirect-mid", follow=False)
        assert status == 302 and headers.get("Location") == "/api/redirect-broken"
        status, _, body = _get(srv.base_url + "/api/redirect-broken", follow=False)
        assert status == 404


def test_attribution_scores():
    s = attribution_scores({"/api/failing-endpoint"}, {"/api/failing-endpoint"})
    assert s["precision"] == 1.0 and s["recall"] == 1.0
    # Attributing the background ping as caused is a false positive.
    s2 = attribution_scores({"/api/failing-endpoint", "/api/background-ping"}, {"/api/failing-endpoint"})
    assert s2["fp"] == 1 and s2["precision"] == 0.5


def test_metric_targets():
    assert dropped_event_rate(0, 1000) == 0.0
    assert dropped_event_rate(5, 1000) == 0.005
    links = [{"confidence": "supported", "evidence": {"url": "http://x/api/background-ping"}}]
    assert false_supported_rate(links, ["/api/background-ping"]) == 1.0
    assert false_supported_rate([], ["/api/background-ping"]) == 0.0
