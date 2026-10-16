"""Cycle 44 tests: exception metadata + backendNodeId HTML fallback."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.dom import DOMEngine
from agent_browser.runtime import RuntimeEngine


@pytest.mark.asyncio
async def test_exception_metadata_persisted():
    eng = RuntimeEngine(None, lambda: "sid")
    await eng.handle_cdp_event({
        "method": "Runtime.exceptionThrown",
        "params": {
            "timestamp": 1.0,
            "exceptionDetails": {
                "text": "Uncaught (in promise) TypeError: boom",
                "exceptionId": 42,
                "scriptId": "99",
                "url": "https://x/app.js",
                "lineNumber": 3,
                "columnNumber": 5,
                "exception": {"className": "TypeError", "description": "TypeError: boom"},
                "stackTrace": {"callFrames": []},
            },
        },
    })
    entry = eng.get_exceptions()[-1]
    assert entry["class_name"] == "TypeError"
    assert entry["script_id"] == "99"
    assert entry["exception_id"] == 42
    assert entry["origin"] == "rejection"


class _FallbackCDP:
    def __init__(self):
        self.sent = []

    async def send(self, method, params=None, session_id=None, timeout=15):
        self.sent.append((method, params or {}))
        if method == "Runtime.evaluate":
            return {"result": {"value": ""}}  # force the DOM fallback path
        if method == "DOM.getDocument":
            assert params.get("depth") == 0, "fallback must not fetch the whole tree"
            return {"root": {"backendNodeId": 777, "nodeId": 1}}
        if method == "DOM.getOuterHTML":
            assert params.get("backendNodeId") == 777
            return {"outerHTML": "<html>fallback</html>"}
        return {}


@pytest.mark.asyncio
async def test_page_html_fallback_uses_backend_id():
    dom = DOMEngine(_FallbackCDP(), lambda: "sid")
    html = await dom.get_page_html()
    assert html == "<html>fallback</html>"
    methods = [m for m, _ in dom.cdp.sent]
    assert "DOM.getDocument" in methods and "DOM.getOuterHTML" in methods
