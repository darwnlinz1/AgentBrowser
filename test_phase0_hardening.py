"""
Phase 0 hardening tests: loopback security helpers, output-path confinement,
daemon token round-trip, and on-egress secret redaction. No browser launched.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser import security
from agent_browser.evidence import redact_entry


def test_host_allowed_loopback_only():
    assert security.host_allowed("127.0.0.1:9224")
    assert security.host_allowed("localhost:9224")
    assert security.host_allowed("[::1]:9224")
    assert security.host_allowed(None)  # header-less local client
    assert not security.host_allowed("evil.example.com")
    assert not security.host_allowed("evil.example.com:9224")


def test_origin_rejects_cross_site():
    assert security.origin_allowed(None)
    assert security.origin_allowed("http://127.0.0.1:9224")
    assert security.origin_allowed("http://localhost")
    assert not security.origin_allowed("https://evil.example.com")
    assert not security.origin_allowed("http://attacker.test:9224")


def test_bearer_ok():
    assert security.bearer_ok("Bearer abc", "abc")
    assert security.bearer_ok("bearer abc", "abc")
    assert not security.bearer_ok("Bearer wrong", "abc")
    assert not security.bearer_ok(None, "abc")
    assert not security.bearer_ok("abc", "abc")  # missing scheme
    assert security.bearer_ok(None, None)  # no token configured => open
    assert security.bearer_ok("anything", "")  # empty expected => open


def test_token_roundtrip(monkeypatch):
    monkeypatch.delenv("AGENTBROWSER_TOKEN", raising=False)
    port = 59999
    tok = security.load_or_create_token(port)
    assert tok
    assert security.read_token(port) == tok
    try:
        os.remove(security.token_path(port))
    except OSError:
        pass


def test_token_env_override(monkeypatch):
    monkeypatch.setenv("AGENTBROWSER_TOKEN", "env-token-xyz")
    assert security.read_token(12345) == "env-token-xyz"
    assert security.load_or_create_token(12345) == "env-token-xyz"


def test_confine_path_blocks_escape(tmp_path):
    root = str(tmp_path / "out")
    p = security.confine_path("shot.png", root)
    assert os.path.abspath(p).startswith(os.path.abspath(root))
    p2 = security.confine_path("sub/dir/a.png", root)
    assert os.path.isdir(os.path.dirname(p2))
    with pytest.raises(ValueError):
        security.confine_path("../../etc/passwd", root)
    with pytest.raises(ValueError):
        security.confine_path(os.path.abspath(os.path.join(root, "..", "evil.txt")), root)


def test_redact_entry_masks_secrets_keeps_body():
    entry = {
        "id": "1",
        "url": "https://api.example/me",
        "method": "GET",
        "headers": {"Cookie": "sid=secret", "Authorization": "Bearer t", "Accept": "application/json"},
        "request_cookie_header": "sid=secret",
        "response_headers_raw": [
            {"name": "Set-Cookie", "value": "a=b"},
            {"name": "Content-Type", "value": "application/json"},
        ],
        "request_cookies": {"sid": "secret"},
        "response_cookies": {"x": "y"},
        "request_body": "payload-keep",
        "response_body": '{"ok":true}',
    }
    red = redact_entry(entry)
    assert red["headers"]["Cookie"] == "[redacted]"
    assert red["headers"]["Authorization"] == "[redacted]"
    assert red["headers"]["Accept"] == "application/json"
    assert red["request_cookie_header"] == "[redacted]"
    assert red["request_cookies"] == "[redacted]"
    assert red["response_cookies"] == "[redacted]"
    set_cookie = [h for h in red["response_headers_raw"] if h["name"] == "Set-Cookie"][0]
    assert set_cookie["value"] == "[redacted]"
    # Body + URL preserved for debugging.
    assert red["request_body"] == "payload-keep"
    assert red["response_body"] == '{"ok":true}'
    assert red["url"] == "https://api.example/me"
    # Original entry is not mutated.
    assert entry["headers"]["Authorization"] == "Bearer t"
