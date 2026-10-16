"""Cycle 10 tests: curl reproduction synthesis."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.synth import build_curl

ENTRY = {
    "method": "GET",
    "url": "https://api.example.com/orders/123?sort=asc",
    "headers": {"Accept": "application/json", "Cookie": "sid=topsecret", "Authorization": "Bearer abc123"},
    "request_body": "",
}


def test_curl_secrets_become_env_placeholders():
    out = build_curl(ENTRY)
    assert out["refused"] is False
    assert "topsecret" not in out["command_posix"]
    assert "abc123" not in out["command_windows"]
    assert "AB_COOKIE" in out["env_required"]
    assert "AB_AUTHORIZATION" in out["env_required"]
    assert "${AB_COOKIE}" in out["command_posix"]
    assert "/orders/{n}" in out["url_template"]


def test_curl_write_refused_without_opt_in():
    post = {**ENTRY, "method": "POST", "request_body": '{"a":1}'}
    assert build_curl(post)["refused"] is True
    ok = build_curl(post, allow_write=True)
    assert ok["refused"] is False
    assert "--data-binary" in ok["command_posix"]


def test_curl_include_secrets_only_when_explicit():
    out = build_curl(ENTRY, include_secrets=True)
    assert "topsecret" in out["command_posix"]
    assert out["env_required"] == []
