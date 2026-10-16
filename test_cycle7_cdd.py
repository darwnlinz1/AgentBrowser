"""Cycle 7 tests: CDD minimizer + normalized signature templates."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.diagnostics import DiagnosticEngine
from agent_browser.evidence import normalize_exception_message, url_template
from agent_browser.minimizer import cdd, ddmin, minimize_journal


def test_url_template_collapses_ids_and_volatile_keys():
    a = url_template("https://api.example.com/orders/12345?sort=asc&_ =1&nonce=xyz")
    b = url_template("https://api.example.com/orders/999?sort=desc&_=2")
    assert "/orders/{n}" in a
    assert "nonce" not in a and "_" not in a.split("?")[-1]
    # Query values are blanked, so id/token churn maps to one template.
    assert a.split("?")[0] == b.split("?")[0]
    assert url_template("https://x/users/3fa85f64-5717-4562-b3fc-2c963f66afa6") .endswith("/users/{uuid}")


def test_normalize_exception_message_keeps_meaningful_slots():
    out = normalize_exception_message("TypeError: Cannot read properties of undefined (reading 'userId') at line 42")
    assert out["template_id"] == "V8.NonObjectPropertyLoad"
    assert "userId" in out["template"]
    generic = normalize_exception_message("Request 98765 failed after 3 retries")
    assert "<num>" in generic["template"]


def test_failure_signature_uses_templates():
    sig = DiagnosticEngine.failure_signature(
        [{"description": "TypeError: Cannot read properties of undefined (reading 'id')"}],
        [{"url": "https://api.example/orders/123?x=1", "status": 500}],
    )
    assert sig["exception_template_id"] == "V8.NonObjectPropertyLoad"
    assert "{n}" in sig["failing_request"]["path"]


def test_cdd_isolates_essential_and_is_one_minimal():
    # Note: on a single-essential needle ddmin's log search is already optimal;
    # CDD's query advantage shows on larger multi-essential workloads (ICSE
    # 2025). This test pins correctness + 1-minimality, not a query race.
    items = [{"id": i, "prior": 0.05} for i in range(24)] + [{"id": "essential", "prior": 0.95}]

    def make_pred(counter):
        def pred(candidate):
            counter["n"] += 1
            return any(isinstance(x, dict) and x.get("id") == "essential" for x in candidate)
        return pred

    via_ddmin = ddmin(items, make_pred({"n": 0}))
    via_cdd = cdd(items, make_pred({"n": 0}))
    assert [x["id"] for x in via_ddmin] == ["essential"]
    assert [x["id"] for x in via_cdd] == ["essential"]
    # Two essentials among many noise units, priors marking them essential-last.
    items2 = ([{"id": f"n{i}", "prior": 0.03} for i in range(30)]
              + [{"id": "e1", "prior": 0.9}, {"id": "e2", "prior": 0.9}])

    def pred2(candidate):
        ids = {x.get("id") for x in candidate}
        return {"e1", "e2"} <= ids

    assert sorted(x["id"] for x in cdd(items2, pred2)) == ["e1", "e2"]
    out = minimize_journal(items, make_pred({"n": 0}), trials=1, algorithm="cdd")
    assert out["minimal_count"] == 1 and out["algorithm"] == "cdd"
