"""Cycle 59 tests: per-socket sequence numbers for WS frames and SSE."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.network import NetworkEngine


def _engine():
    return NetworkEngine(None, lambda: "S1")


def _ws(rid, direction, payload="{}"):
    method = "Network.webSocketFrameSent" if direction == "sent" else "Network.webSocketFrameReceived"
    return {"method": method, "params": {
        "requestId": rid, "timestamp": 1.0,
        "response": {"opcode": 1, "payloadData": payload}}}


@pytest.mark.asyncio
async def test_ws_frames_get_per_socket_sequences():
    eng = _engine()
    for ev in (_ws("A", "sent"), _ws("B", "received"), _ws("A", "received"),
               _ws("A", "sent"), _ws("B", "sent")):
        await eng.handle_cdp_event(ev)
    a_seqs = [f["seq"] for f in eng.ws_frames if f["requestId"] == "A"]
    b_seqs = [f["seq"] for f in eng.ws_frames if f["requestId"] == "B"]
    assert a_seqs == [1, 2, 3]
    assert b_seqs == [1, 2]
    sock_a = eng.ws_sockets["A"]
    assert sock_a["frames_sent"] == 2 and sock_a["frames_received"] == 1


@pytest.mark.asyncio
async def test_sse_messages_get_per_stream_sequences():
    eng = _engine()
    for i in range(3):
        await eng.handle_cdp_event({"method": "Network.eventSourceMessageReceived",
                                    "params": {"requestId": "E1", "data": f"msg-{i}",
                                               "eventName": "tick", "eventId": str(i)}})
    await eng.handle_cdp_event({"method": "Network.eventSourceMessageReceived",
                                "params": {"requestId": "E2", "data": "other",
                                           "eventName": "tick", "eventId": "0"}})
    assert [m["seq"] for m in eng.sse_messages if m["requestId"] == "E1"] == [1, 2, 3]
    assert [m["seq"] for m in eng.sse_messages if m["requestId"] == "E2"] == [1]
