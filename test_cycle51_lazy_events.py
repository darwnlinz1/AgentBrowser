"""Cycle 51 tests: method-interest registry skips full parse for unwanted events."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.cdp import CDPConnection
from agent_browser.quiescence import NetworkActivityMonitor
from agent_browser.transport import PipeCDPClient


class _WS:
    def __init__(self, msgs):
        self._msgs = list(msgs)

    async def recv(self):
        if self._msgs:
            return self._msgs.pop(0)
        await asyncio.sleep(60)

    async def close(self):
        pass


async def _pump(conn, msgs, settle=0.15):
    conn.ws = _WS(msgs)
    conn.running = True
    task = asyncio.create_task(conn._listen_loop())
    await asyncio.sleep(settle)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


_NOISE = '{"method": "Page.lifecycleEvent", "params": {"name": "x", "blob": "%s"}}' % ("y" * 500)
_REQ = '{"method": "Network.requestWillBeSent", "params": {"requestId": "r1"}}'


@pytest.mark.asyncio
async def test_unwanted_events_skip_full_parse():
    conn = CDPConnection("ws://127.0.0.1:9/x")
    seen = []
    conn.add_sync_handler(lambda d: seen.append(d.get("method")),
                          methods={"Network.requestWillBeSent"})
    await _pump(conn, [_NOISE, _REQ, _NOISE])
    assert seen == ["Network.requestWillBeSent"]
    assert conn.skipped_events == 2
    assert conn.sync_handler_errors == 0


@pytest.mark.asyncio
async def test_catch_all_handler_still_receives_everything():
    conn = CDPConnection("ws://127.0.0.1:9/x")
    seen = []

    async def handler(data):
        seen.append(data.get("method"))

    conn.add_event_handler(handler)
    await _pump(conn, [_NOISE, _REQ])
    assert seen == ["Page.lifecycleEvent", "Network.requestWillBeSent"]
    assert conn.skipped_events == 0


@pytest.mark.asyncio
async def test_monitor_ledger_via_declared_methods():
    conn = CDPConnection("ws://127.0.0.1:9/x")
    monitor = NetworkActivityMonitor()
    conn.add_sync_handler(monitor.on_event, methods=NetworkActivityMonitor.CDP_METHODS)
    await _pump(conn, [
        _NOISE,
        '{"method": "Network.requestWillBeSent", "params": {"requestId": "r9", '
        '"type": "XHR", "request": {"url": "https://x/api", "method": "GET"}}}',
        '{"method": "Network.loadingFinished", "params": {"requestId": "r9"}}',
    ])
    assert monitor.in_flight == set()
    assert monitor.activity_seq >= 2
    assert conn.skipped_events == 1


@pytest.mark.asyncio
async def test_removing_handler_restores_skip():
    conn = CDPConnection("ws://127.0.0.1:9/x")
    seen = []
    handler = lambda d: seen.append(d.get("method"))  # noqa: E731
    conn.add_event_handler(handler, methods={"Page.lifecycleEvent"})
    conn.remove_event_handler(handler)
    await _pump(conn, [_NOISE], settle=0.1)
    assert seen == []
    assert conn.skipped_events == 1


def test_envelope_decodes_replies_and_events():
    # Regression: Raw | None fields made msgspec reject every non-empty
    # frame, so decode_envelope returned None and envelope routing was dead.
    from agent_browser import codec

    reply = codec.decode_envelope('{"id": 7, "result": {"x": 1}}')
    assert reply is not None and reply.id == 7 and reply.method is None
    assert codec.decode_raw(reply.result) == {"x": 1}
    assert not reply.error

    event = codec.decode_envelope(
        '{"method": "Network.requestWillBeSent", "params": {"requestId": "r1"}, "sessionId": "S"}')
    assert event is not None and event.method == "Network.requestWillBeSent"
    assert event.sessionId == "S"
    assert codec.decode_raw(event.params) == {"requestId": "r1"}


def test_pipe_client_dispatch_filters_by_method():
    client = PipeCDPClient(0, 0)
    seen = []
    client.add_sync_handler(lambda d: seen.append(d.get("method")),
                            methods={"Network.requestWillBeSent"})
    client._dispatch({"method": "Page.lifecycleEvent", "params": {}})
    client._dispatch({"method": "Network.requestWillBeSent", "params": {}})
    assert seen == ["Network.requestWillBeSent"]
    assert client.sync_handler_errors == 0
