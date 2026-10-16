"""Cycle 56 tests: portable vs strict failure-signature strengths."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.diagnostics import DiagnosticEngine
from agent_browser.evidence import url_path_template
from agent_browser.loop_engine import symptom_matches, trace_signature


def _trace(url, status, exc="TypeError: Cannot read properties of undefined (reading 'id')"):
    return {
        "network": {"requests": [{"url": url, "status": status}]},
        "runtime": {"exceptions": [{"description": exc, "text": "TypeError"}]},
    }


def test_url_path_template_strips_host_and_port():
    assert url_path_template("http://127.0.0.1:54321/api/users/42?token=x") == "/api/users/{n}?token="
    assert url_path_template("https://staging.example.com/api/data") == "/api/data"
    assert url_path_template("") == ""


def test_portable_survives_port_and_status_drift_strict_does_not():
    sig = trace_signature(_trace("http://127.0.0.1:5001/api/failing-endpoint", 500))
    assert sig["failing_request"]["path_only"] == "/api/failing-endpoint"
    drifted = _trace("http://localhost:9999/api/failing-endpoint", 502)
    assert symptom_matches(drifted, sig) is False                 # strict default
    assert symptom_matches(drifted, sig, "portable") is True
    same = _trace("http://127.0.0.1:5001/api/failing-endpoint", 500)
    assert symptom_matches(same, sig) is True
    assert symptom_matches(same, sig, "portable") is True


def test_portable_still_requires_exception_class_and_path():
    sig = trace_signature(_trace("http://h:1/api/failing-endpoint", 500))
    wrong_exc = _trace("http://other:2/api/failing-endpoint", 503,
                       exc="ReferenceError: x is not defined")
    assert symptom_matches(wrong_exc, sig, "portable") is False
    wrong_path = _trace("http://other:2/api/other-endpoint", 503)
    assert symptom_matches(wrong_path, sig, "portable") is False
    not_failing = _trace("http://other:2/api/failing-endpoint", 200)
    assert symptom_matches(not_failing, sig, "portable") is False


def test_unknown_strength_rejected():
    sig = trace_signature(_trace("http://h/api/x", 500))
    with pytest.raises(ValueError, match="signature strength"):
        symptom_matches(_trace("http://h/api/x", 500), sig, "loose")


def test_signature_carries_path_only():
    sig = DiagnosticEngine.failure_signature(
        [{"description": "TypeError: boom", "text": "TypeError"}],
        [{"url": "http://127.0.0.1:7777/api/items/5", "status": 404}])
    assert sig["failing_request"]["path"] == "127.0.0.1:7777/api/items/{n}"
    assert sig["failing_request"]["path_only"] == "/api/items/{n}"
