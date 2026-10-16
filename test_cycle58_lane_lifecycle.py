"""Cycle 58 tests: FIFO lanes die with their socket, counted not silent."""

import asyncio
import os
import sys

import pytest
import websockets

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.cdp import CDPConnection
from agent_browser.transport import PipeCDPClient


class _ClosingWS:
    def __init__(self, msgs):
        self._msgs = list(msgs)

    async def recv(self):
        await asyncio.sleep(0)  # a real socket yields; let the lane worker run
        if self._msgs:
            return self._msgs.pop(0)
        raise websockets.ConnectionClosed(None, None)

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_lanes_close_on_socket_drop_and_queued_calls_never_run():
    conn = CDPConnection("ws://127.0.0.1:9/x")
    gate = asyncio.Event()
    ran = []

    async def handler(data):
        ran.append(data["params"]["requestId"])
        await gate.wait()

    conn.add_event_handler(handler)
    msgs = [
        '{"method": "Network.requestWillBeSent", "sessionId": "S", "params": {"requestId": "r1"}}',
        '{"method": "Network.requestWillBeSent", "sessionId": "S", "params": {"requestId": "r2"}}',
        '{"method": "Network.requestWillBeSent", "sessionId": "S", "params": {"requestId": "r3"}}',
    ]
    conn.ws = _ClosingWS(msgs)
    conn.running = True
    await conn._listen_loop()
    # r1 started and was cancelled with the lane; r2/r3 were still queued,
    # so they were closed (counted as dropped) and never executed.
    assert ran == ["r1"]
    assert conn.dropped_events == 2
    assert conn._lanes == {} and conn._lane_workers == {}
    gate.set()


@pytest.mark.asyncio
async def test_pipe_disconnect_closes_lanes():
    client = PipeCDPClient(0, 0)
    ran = []

    async def handler(obj):
        ran.append(obj["method"])

    client._loop = asyncio.get_running_loop()
    client._lane_put("S", handler, {"method": "Network.requestWillBeSent"})
    await asyncio.sleep(0.05)
    assert ran == ["Network.requestWillBeSent"]
    await client.disconnect()
    assert client._lanes == {} and client._lane_workers == {}
