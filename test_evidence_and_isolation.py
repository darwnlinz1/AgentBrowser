"""
Unit tests for evidence labeling, profile containment, CDP shutdown,
cookie URL construction, and redirect recording.

No browser is launched. CDP is a fake websocket.
"""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.cdp import CDPConnection, resolve_profile_dir
from agent_browser.diagnostics import DiagnosticEngine
from agent_browser.evidence import (
    build_causal_links,
    build_trace_report,
    cookie_url_for_domain,
    is_within_dir,
    slice_since,
    summarize_request,
)
from agent_browser.network import NetworkEngine
from agent_browser.source_inspector import SourceInspector
from agent_browser.storage import StorageEngine


class _FakeWS:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if not self.incoming:
            await asyncio.sleep(60)
        item = self.incoming.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        self.closed = True


class _FakeRuntime:
    def __init__(self, exceptions=None, logs=None):
        self.exceptions = exceptions or []
        self.console_logs = logs or []

    def get_exceptions(self, limit=20):
        return self.exceptions[-limit:]

    def get_console_messages(self, level=None, limit=50):
        return [l for l in self.console_logs if l.get("type") == level][-limit:]


class _FakeNetwork:
    def __init__(self, traffic):
        self.traffic = traffic

    def search_traffic(self, limit=30):
        return self.traffic[-limit:]


class _FakeDOM:
    async def get_interactive_snapshot(self):
        return {"url": "http://test.local/", "title": "t"}


def test_causal_links_do_not_pair_unrelated_failures():
    exceptions = [{
        "description": "TypeError: Cannot read properties of undefined (reading 'id')",
        "url": "https://app.example/app.js",
        "line": 10,
    }]
    requests = [
        {"id": "a", "method": "GET", "url": "https://api.example/orders", "status": 500},
        {"id": "b", "method": "GET", "url": "https://cdn.example/pixel", "status": 404},
        {"id": "c", "method": "POST", "url": "https://api.example/cart", "status": 403},
    ]
    links = build_causal_links(exceptions, requests)
    assert links == []


def test_causal_link_when_exception_names_the_failing_url():
    exceptions = [{
        "description": "Failed to fetch https://api.example/orders/12",
        "url": "https://app.example/app.js",
    }]
    requests = [
        {"id": "a", "method": "GET", "url": "https://api.example/orders/12", "status": 500},
        {"id": "b", "method": "GET", "url": "https://cdn.example/pixel", "status": 404},
    ]
    links = build_causal_links(exceptions, requests)
    assert len(links) == 1
    assert links[0]["confidence"] == "supported"
    assert links[0]["kind"] == "url_overlap"
    assert links[0]["evidence"]["request_id"] == "a"


def test_weak_link_only_when_exactly_one_failure():
    ex = [{"description": "Cannot read properties of undefined (reading 'x')", "url": "app.js"}]
    one = [{"id": "only", "url": "https://api.example/user", "status": 500, "method": "GET"}]
    links = build_causal_links(ex, one)
    assert len(links) == 1
    assert links[0]["confidence"] == "weak"

    two = one + [{"id": "other", "url": "https://api.example/other", "status": 502, "method": "GET"}]
    assert build_causal_links(ex, two) == []
    # A favicon 404 must not cancel the single API failure.
    with_favicon = one + [{"id": "ico", "url": "https://app.example/favicon.ico", "status": 404, "method": "GET", "resource_type": "Image"}]
    fav = build_causal_links(ex, with_favicon)
    assert len(fav) == 1
    assert fav[0]["evidence"]["request_id"] == "only"


def test_trace_report_does_not_claim_the_action_triggered_background_traffic():
    report = build_trace_report(
        action_type="click",
        target="#btn",
        params={},
        action_error=None,
        action_result={"clicked": True},
        start_time=1.0,
        duration_ms=1000,
        pre_url="http://t/",
        post_url="http://t/",
        post_title="t",
        mutations=[],
        mutation_truncated=False,
        observed_requests=[summarize_request(
            {"id": "bg", "url": "https://ads.example/p", "method": "GET", "status": 200, "initiator": {}},
            "requestWillBeSent appended after index snapshot",
        )],
        observed_exceptions=[],
        observed_console_errors=[],
        window_note="test",
    )
    blob = " ".join(report["diagnostics"]).lower()
    assert "triggered" not in blob
    assert report["network"]["requests_observed_count"] == 1
    # A settle-window request is not a trigger, including a weak null-consumer link.
    assert report["network"]["requests_triggered_count"] == 0
    assert report["network"]["requests"][0]["confidence"] == "temporal"
    assert report["evidence"]["causal_links"] == []
    assert report["action"]["success"] is True
    assert report["action"]["listener_reached"] is False
    assert report["action"]["page_idle"] is True


def test_weak_link_does_not_count_as_triggered():
    from agent_browser.evidence import build_trace_report, summarize_request
    report = build_trace_report(
        action_type="click",
        target="#btn",
        params={},
        action_error=None,
        action_result={"clicked": True},
        start_time=1.0,
        duration_ms=1000,
        pre_url="http://t/",
        post_url="http://t/",
        post_title="t",
        mutations=[],
        mutation_truncated=False,
        observed_requests=[
            summarize_request(
                {"id": "api", "url": "https://api.example/fail", "method": "GET", "status": 500, "resource_type": "Fetch", "initiator": {}},
                "settle window",
            ),
            summarize_request(
                {"id": "ping", "url": "https://app.example/api/background-ping", "method": "GET", "status": 200, "resource_type": "Fetch", "initiator": {}},
                "settle window",
            ),
        ],
        observed_exceptions=[{
            "description": "TypeError: Cannot read properties of undefined (reading 'id')",
            "url": "http://t/",
            "text": "TypeError",
        }],
        observed_console_errors=[],
        window_note="test",
    )
    assert report["network"]["requests_observed_count"] == 2
    assert report["evidence"]["link_count"] == 1
    assert report["evidence"]["causal_links"][0]["confidence"] == "weak"
    assert report["network"]["requests_triggered_count"] == 0
    assert all(r["confidence"] == "temporal" for r in report["network"]["requests"])
    assert report["action"]["listener_reached"] is False
    assert report["action"]["page_idle"] is False


def test_click_report_marks_listener_reached_when_page_changes():
    report = build_trace_report(
        action_type="click",
        target="#2",
        params={},
        action_error=None,
        action_result={"clicked": True, "element": {"id": "fetch-btn"}},
        start_time=1.0,
        duration_ms=1500,
        pre_url="http://127.0.0.1:9/",
        post_url="http://127.0.0.1:9/",
        post_title="t",
        mutations=[{"type": "text", "text": "clicked"}],
        mutation_truncated=False,
        observed_requests=[summarize_request(
            {"id": "ping", "url": "http://127.0.0.1:9/api/background-ping", "method": "GET", "status": 200, "initiator": {}},
            "settle window",
        )],
        observed_exceptions=[{
            "description": "TypeError: Cannot read properties of undefined (reading 'id')",
            "url": "http://127.0.0.1:9/",
            "text": "TypeError",
        }],
        observed_console_errors=[],
        window_note="test",
    )
    assert report["action"]["listener_reached"] is True
    assert report["action"]["page_idle"] is False
    assert report["dom_changes"]["mutation_count"] == 1
    assert report["runtime"]["exceptions_count"] == 1
    assert report["network"]["requests_triggered_count"] == 0
    assert "Listener reached" in " ".join(report["diagnostics"])


def test_slice_since_does_not_shift_after_front_eviction():
    items = list(range(10))
    # Caller snapshotted index 8, then the buffer was trimmed below that.
    assert slice_since(items, 8) == [8, 9]
    assert slice_since(items[:3], 8) == []
    assert slice_since(items, 0) == items


def test_profile_names_stay_inside_profiles_root():
    name, path = resolve_profile_dir(profile="dev1")
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "profiles"))
    assert name == "dev1"
    assert is_within_dir(path, root)
    assert os.path.basename(path) == "dev1"

    # Traversal and sibling-prefix names cannot escape.
    for dirty in ("../escape", "..\\escape", "profiles_evil", "a/../../outside", "  "):
        n, p = resolve_profile_dir(profile=dirty)
        assert is_within_dir(p, root), (dirty, p)
        assert os.path.abspath(p) != os.path.abspath(os.path.dirname(root))


def test_cookie_url_does_not_treat_path_as_port():
    assert cookie_url_for_domain("127.0.0.1", "/") == "http://127.0.0.1/"
    assert cookie_url_for_domain("127.0.0.1:8080", "/") == "http://127.0.0.1:8080/"
    assert cookie_url_for_domain("localhost", "/shop") == "http://localhost/shop"
    # The old bug appended the path after a colon: http://127.0.0.1:8080:/
    assert not cookie_url_for_domain("127.0.0.1:8080", "/").endswith(":/")


def test_diagnostics_does_not_emit_false_root_cause():
    engine = DiagnosticEngine(
        _FakeDOM(),
        _FakeNetwork([
            {"id": "a", "method": "GET", "url": "https://api.example/orders", "status": 500, "response_body": "no"},
            {"id": "b", "method": "GET", "url": "https://cdn.example/x", "status": 404, "response_body": ""},
        ]),
        _FakeRuntime(
            exceptions=[{"description": "Cannot read properties of undefined", "url": "app.js", "line": 1, "column": 1, "text": "TypeError"}],
            logs=[],
        ),
        None,
        None,
    )

    async def _run():
        return await engine.run_diagnostics()

    report = asyncio.run(_run())
    assert report["health"] == "UNHEALTHY"
    blob = " ".join(report["root_cause_analysis"]).lower()
    assert "likely caused" not in blob
    assert "insufficient_evidence" in blob or "not attributed" in blob
    assert all(h["confidence"] != "supported" for h in report["hypotheses"])


def test_diagnose_keeps_single_api_signal_when_favicon_and_unsafe_port_fail():
    engine = DiagnosticEngine(
        _FakeDOM(),
        _FakeNetwork([
            {"id": "nav", "method": "GET", "url": "http://127.0.0.1:1/", "status": "FAILED", "resource_type": "Document", "error_text": "net::ERR_UNSAFE_PORT"},
            {"id": "icon", "method": "GET", "url": "http://127.0.0.1:9/favicon.ico", "status": 404, "resource_type": "Other"},
            {"id": "api", "method": "GET", "url": "http://127.0.0.1:9/api/failing-endpoint", "status": 500, "resource_type": "Fetch", "response_body": '{"error":"nope"}'},
        ]),
        _FakeRuntime(
            exceptions=[{
                "description": "TypeError: Cannot read properties of undefined (reading 'id')",
                "url": "http://127.0.0.1:9/",
                "line": 17,
                "column": 67,
                "text": "TypeError",
            }],
            logs=[],
        ),
        None,
        None,
    )
    report = asyncio.run(engine.run_diagnostics())
    assert any("Primary actionable failure" in line and "api/failing-endpoint" in line for line in report["root_cause_analysis"])
    assert any(h.get("kind") == "single_failure_null_consumer" and h.get("confidence") == "weak" for h in report["hypotheses"])
    assert not any(h.get("status") == "insufficient_evidence" for h in report["hypotheses"])
    # Failure details must not be the raw request dict (which would include cookies).
    for issue in report["issues"]:
        if issue["category"] == "NETWORK_FAILURE":
            assert "headers" not in issue["details"]


def test_reproduction_script_escapes_quotes():
    engine = DiagnosticEngine(None, None, None, None, None)

    async def _run():
        return await engine.generate_reproduction_script(
            'http://evil.test/?q="injected',
            {"type": "click", "target": '"); import os; os.system("calc'},
        )

    script = asyncio.run(_run())
    compile(script, "<repro>", "exec")
    # The payload stays inside a string literal. It is not a statement.
    assert 'selector = "' in script
    assert "\nos.system" not in script
    assert "import os" not in script.split("import asyncio", 1)[0]


@pytest.mark.asyncio
async def test_cdp_disconnect_fails_pending_send():
    conn = CDPConnection("ws://127.0.0.1:9/devtools/browser/test")
    ws = _FakeWS([])
    conn.ws = ws
    conn.running = True

    async def _never_recv():
        await asyncio.sleep(30)

    conn._listener_task = asyncio.create_task(_never_recv())

    send_task = asyncio.create_task(conn.send("Page.enable", {}, timeout=5))
    await asyncio.sleep(0.05)
    await conn.disconnect()
    with pytest.raises(ConnectionError):
        await send_task
    assert conn.running is False
    assert conn.pending_futures == {}


@pytest.mark.asyncio
async def test_cdp_connection_closed_fails_pending_without_waiting_full_timeout():
    import websockets

    conn = CDPConnection("ws://127.0.0.1:9/devtools/browser/test")
    ws = _FakeWS([websockets.ConnectionClosed(None, None)])
    conn.ws = ws
    conn.running = True
    conn._listener_task = asyncio.create_task(conn._listen_loop())

    started = asyncio.get_event_loop().time()
    with pytest.raises(ConnectionError):
        await conn.send("Runtime.evaluate", {"expression": "1"}, timeout=8)
    elapsed = asyncio.get_event_loop().time() - started
    assert elapsed < 2.0
    assert conn.running is False


@pytest.mark.asyncio
async def test_extra_info_arriving_first_is_not_dropped():
    engine = NetworkEngine(None, lambda: "sid")
    await engine.handle_cdp_event({
        "method": "Network.requestWillBeSentExtraInfo",
        "params": {
            "requestId": "r1",
            "headers": {"Cookie": "session=secret", "User-Agent": "t"},
            "associatedCookies": [{"cookie": {"name": "session"}, "blockedReasons": []}],
        },
    })
    await engine.handle_cdp_event({
        "method": "Network.responseReceivedExtraInfo",
        "params": {
            "requestId": "r1",
            "statusCode": 304,
            "headers": {"Set-Cookie": "a=1\nb=2"},
            "headersText": "HTTP/1.1 304 Not Modified\r\n",
        },
    })
    await engine.handle_cdp_event({
        "method": "Network.requestWillBeSent",
        "params": {
            "requestId": "r1",
            "request": {
                "url": "https://shop.example/a",
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "hasPostData": True,
            },
            "wallTime": 1,
            "timestamp": 1,
            "type": "Fetch",
            "initiator": {"type": "script"},
        },
    })
    await engine.handle_cdp_event({
        "method": "Network.responseReceived",
        "params": {
            "requestId": "r1",
            "hasExtraInfo": True,
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": {"Content-Type": "application/json"},
                "mimeType": "application/json",
                "protocol": "h2",
                "remoteIPAddress": "1.2.3.4",
                "remotePort": 443,
            },
        },
    })
    entry = engine.get_request("r1")
    assert entry["request_cookie_header"] == "session=secret"
    assert entry["headers"]["Content-Type"] == "application/json"
    assert entry["status"] == 304
    assert entry["status_from_extra"] == 304
    assert entry["protocol"] == "h2"
    assert entry["remote_ip"] == "1.2.3.4"
    assert entry["response_headers_text"].startswith("HTTP/1.1 304")
    assert entry["post_data_complete"] is False
    assert "post_data_not_inlined" in entry["capture_gaps"]
    names = [h["name"].lower() for h in entry["response_headers_raw"] if h["name"].lower() == "set-cookie"]
    assert names.count("set-cookie") == 2


@pytest.mark.asyncio
async def test_get_response_body_failure_is_recorded():
    class _CDP:
        async def send(self, method, params=None, session_id=None, timeout=15):
            raise RuntimeError("No resource with given identifier found")

    engine = NetworkEngine(_CDP(), lambda: "sid")
    await engine.handle_cdp_event({
        "method": "Network.requestWillBeSent",
        "params": {
            "requestId": "r2",
            "request": {"url": "https://shop.example/a", "method": "GET", "headers": {}, "hasPostData": False},
            "wallTime": 1,
            "timestamp": 1,
            "type": "XHR",
            "initiator": {},
        },
    })
    await engine.handle_cdp_event({
        "method": "Network.loadingFinished",
        "sessionId": "sid",
        "params": {"requestId": "r2", "timestamp": 1.2, "encodedDataLength": 40},
    })
    await asyncio.sleep(0.05)
    entry = engine.get_request("r2")
    assert entry["encoded_data_length"] == 40
    assert entry["body_error"]
    assert "getResponseBody_failed" in entry["capture_gaps"]


@pytest.mark.asyncio
async def test_redirect_does_not_duplicate_request_id():
    engine = NetworkEngine(None, lambda: "sid")
    await engine.handle_cdp_event({
        "method": "Network.requestWillBeSent",
        "params": {
            "requestId": "r1",
            "request": {"url": "https://shop.example/a", "method": "GET", "headers": {}},
            "wallTime": 1,
            "timestamp": 1,
            "type": "Document",
            "initiator": {"type": "other"},
        },
    })
    await engine.handle_cdp_event({
        "method": "Network.requestWillBeSent",
        "params": {
            "requestId": "r1",
            "request": {"url": "https://shop.example/b", "method": "GET", "headers": {}},
            "redirectResponse": {"status": 302, "headers": {"location": "/b"}},
            "wallTime": 2,
            "timestamp": 2,
            "type": "Document",
            "initiator": {"type": "other"},
        },
    })
    assert engine.traffic_order[0] == "r1"
    assert "r1#redirect-0" in engine.traffic_order
    entry = engine.get_request("r1")
    hop = engine.get_request("r1#redirect-0")
    assert entry["url"] == "https://shop.example/b"
    assert entry["redirect_hops"] == ["r1#redirect-0"]
    assert hop["url"] == "https://shop.example/a"
    assert hop["status"] == 302
    assert hop["body_error"] == "redirect_response_has_no_body"


def test_log_entry_added_is_captured():
    from agent_browser.runtime import RuntimeEngine
    engine = RuntimeEngine(None, lambda: "sid")

    async def _run():
        await engine.handle_cdp_event({
            "method": "Log.entryAdded",
            "params": {"entry": {"level": "error", "text": "CORS blocked", "source": "security", "url": "https://api.example"}},
        })

    asyncio.run(_run())
    logs = engine.get_console_messages(level="error")
    assert logs[-1]["text"] == "CORS blocked"
    assert logs[-1]["source"] == "security"


def test_handler_task_limit_drops_instead_of_growing():
    import asyncio as aio

    async def _run():
        conn = CDPConnection("ws://127.0.0.1:9/devtools/browser/test")
        conn._max_handler_tasks = 1
        started = aio.Event()

        async def _slow(_data):
            started.set()
            await aio.sleep(5)

        conn.add_event_handler(_slow)
        first = _slow({})
        conn._handler_tasks.add(aio.create_task(first))
        # A second coroutine must be closed, not scheduled.
        second = _slow({})
        if len(conn._handler_tasks) >= conn._max_handler_tasks:
            second.close()
            dropped = True
        else:
            dropped = False
        assert dropped
        for task in list(conn._handler_tasks):
            task.cancel()
        await aio.sleep(0)

    asyncio.run(_run())


def test_ai_summary_redacts_cookie_header():
    engine = NetworkEngine(None, lambda: None)
    engine.requests["1"] = {
        "id": "1",
        "url": "https://api.example/me",
        "method": "GET",
        "status": 200,
        "mime_type": "application/json",
        "headers": {
            "Cookie": "session=super-secret-value",
            "Authorization": "Bearer raw-token-value",
            "Accept": "application/json",
        },
        "request_cookie_header": "session=super-secret-value",
        "request_body": "",
        "response_body": "{}",
        "initiator": {},
    }
    engine.traffic_order = ["1"]
    summary = engine.format_ai_summary()
    assert "super-secret-value" not in summary
    assert "raw-token-value" not in summary
    assert "https://api.example/me" in summary
    assert "[redacted]" in summary
    # Stored traffic still holds the secret for an explicit include-secrets query.
    assert engine.get_request("1")["headers"]["Authorization"] == "Bearer raw-token-value"


def test_ai_summary_redacts_authorization_on_representative_record():
    from agent_browser.evidence import redact_mapping
    record = {
        "Cookie": "sid=abc",
        "Authorization": "Bearer xyz",
        "X-Request-Id": "keep-me",
    }
    redacted = redact_mapping(record)
    assert redacted["Cookie"] == "[redacted]"
    assert redacted["Authorization"] == "[redacted]"
    assert redacted["X-Request-Id"] == "keep-me"
    assert record["Authorization"] == "Bearer xyz"


class _ReplyWS:
    """A websocket whose recv returns the reply for the command just sent."""

    def __init__(self):
        self.sent = []
        self.closed = False
        self._queue = asyncio.Queue()

    async def send(self, data):
        self.sent.append(data)
        msg = json.loads(data)
        await self._queue.put(json.dumps({"id": msg["id"], "result": {"title": "Live page"}}))

    async def recv(self):
        if self.closed and self._queue.empty():
            raise websockets.ConnectionClosed(None, None)
        return await self._queue.get()

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_old_generation_future_stays_failed_after_reconnect():
    """connect() starts a generation. disconnect() fails that generation's send."""
    import websockets
    conn = CDPConnection("ws://127.0.0.1:9/devtools/browser/test")
    sockets = [_ReplyWS(), _ReplyWS()]

    async def _connect(url, **kwargs):
        return sockets.pop(0)

    import agent_browser.cdp as cdp_mod
    original = cdp_mod.websockets.connect
    cdp_mod.websockets.connect = _connect
    try:
        await conn.connect()
        first_generation = conn.generation
        assert conn.running is True
        # Drop replies on the first socket so the in-flight send stays pending
        # until disconnect fails it.
        first_ws = conn.ws

        async def _hold():
            await asyncio.sleep(30)
            return "{}"

        first_ws.recv = _hold
        send_task = asyncio.create_task(conn.send("Page.enable", {}, timeout=5))
        await asyncio.sleep(0.05)
        issued = [f for f in conn.pending_futures.values() if f._cdp_generation == first_generation]
        assert issued

        await conn.disconnect()
        with pytest.raises(ConnectionError):
            await send_task
        assert isinstance(issued[0].exception(), ConnectionError)

        await conn.connect()
        assert conn.generation != first_generation
        title = await conn.send("Runtime.evaluate", {"expression": "document.title"}, timeout=2)
        assert title["title"] == "Live page"
        assert isinstance(issued[0].exception(), ConnectionError)
        assert issued[0]._cdp_generation == first_generation
        await conn.disconnect()
    finally:
        cdp_mod.websockets.connect = original


def test_launch_refuses_foreign_profile_and_kills_unready_process(monkeypatch, tmp_path):
    from agent_browser.cdp import BrowserProcessManager

    class _Proc:
        def __init__(self):
            self.killed = False
            self.terminated = False
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.returncode = 1
            return self.returncode

    started = {}

    def _popen(cmd, **kwargs):
        proc = _Proc()
        started["cmd"] = cmd
        started["proc"] = proc
        return proc

    monkeypatch.setattr("agent_browser.cdp.subprocess.Popen", _popen)
    monkeypatch.setattr("agent_browser.cdp.time.sleep", lambda *_: None)

    foreign = BrowserProcessManager(
        port=1,
        headless=True,
        profile_dir=str(tmp_path / "wanted"),
        browser_bin=str(tmp_path / "chrome.exe"),
    )
    foreign._version_payload = lambda: {
        "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/x",
        "userDataDir": str(tmp_path / "other"),
    }
    with pytest.raises(RuntimeError, match="different profile"):
        foreign.launch()
    assert "proc" not in started

    same = BrowserProcessManager(
        port=1,
        headless=True,
        profile_dir=str(tmp_path / "wanted"),
        browser_bin=str(tmp_path / "chrome.exe"),
    )
    same._version_payload = lambda: {
        "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/x",
        "userDataDir": str(tmp_path / "wanted"),
    }
    url = same.launch()
    assert url.endswith("/devtools/browser/x")
    assert same.attached_existing is True
    assert "proc" not in started

    unready = BrowserProcessManager(
        port=1,
        headless=True,
        profile_dir=str(tmp_path / "fresh"),
        browser_bin=str(tmp_path / "chrome.exe"),
    )
    unready._launch_attempts = 2
    unready._version_payload = lambda: None
    with pytest.raises(TimeoutError):
        unready.launch("about:blank")
    assert started["proc"].terminated or started["proc"].killed
    assert unready.proc is None
    assert any("--user-data-dir=" in part for part in started["cmd"])


@pytest.mark.asyncio
async def test_frame_navigated_refreshes_debugger_and_script_source_uses_new_id():
    """
    Top-frame navigation clears the table. scriptParsed that already fired
    is not still in the table. The shipped handler must disable and enable
    Debugger, then script-source must work from the id Chrome emits afterward.
    """
    sent = []
    scripts_after_enable = [{
        "method": "Debugger.scriptParsed",
        "params": {
            "scriptId": "91",
            "url": "http://127.0.0.1:9/encrypt.js",
            "startLine": 0,
            "endLine": 12,
        },
    }]

    class _CDP:
        async def send(self, method, params=None, session_id=None, timeout=15):
            sent.append(method)
            if method == "Debugger.getScriptSource":
                return {"scriptSource": "function encryptPayload(plaintext) { return plaintext; }"}
            if method == "Debugger.enable":
                for event in scripts_after_enable:
                    await inspector.handle_cdp_event(event)
            return {}

    inspector = SourceInspector(_CDP(), lambda: "sid")
    await inspector.handle_cdp_event({
        "method": "Debugger.scriptParsed",
        "sessionId": "sid",
        "params": {"scriptId": "3", "url": "http://127.0.0.1:9/encrypt.js", "startLine": 0, "endLine": 4},
    })
    assert "3" in inspector.scripts
    await inspector.handle_cdp_event({
        "method": "Page.frameNavigated",
        "sessionId": "sid",
        "params": {"frame": {"id": "top"}},
    })
    assert inspector.scripts == {}
    code = await inspector.get_script_source("91")
    assert "function encryptPayload" in code
    assert sent[:2] == ["Debugger.disable", "Debugger.enable"]
    assert "Debugger.getScriptSource" in sent


@pytest.mark.asyncio
async def test_storage_host_port_cookie_url():
    sent = {}

    class _CDP:
        async def send(self, method, params=None, session_id=None, timeout=15):
            sent["method"] = method
            sent["params"] = params
            return {"success": True}

    engine = StorageEngine(_CDP(), lambda: "sid")
    ok = await engine.set_cookie("a", "b", domain="127.0.0.1:8080", path="/")
    assert ok is True
    assert sent["params"]["url"] == "http://127.0.0.1:8080/"
