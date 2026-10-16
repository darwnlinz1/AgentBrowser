"""Cycle 41 tests: WebSocket/SSE semantic decoding and capture."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.network import NetworkEngine
from agent_browser.stream_decode import decode_ws_payload, parse_sse_chunk


def test_decode_socketio_json_and_heartbeat():
    out = decode_ws_payload('42["chat",{"text":"hello"}]', opcode=1)
    assert out["kind"] == "socketio"
    assert out["event"] == "chat"
    assert out["data"] == {"text": "hello"}
    assert decode_ws_payload("2", opcode=1)["heartbeat"] is True
    assert decode_ws_payload('{"a":1}', opcode=1)["kind"] == "json"
    assert decode_ws_payload("plain", opcode=1)["kind"] == "text"
    assert decode_ws_payload("AAAA", opcode=2)["kind"] == "binary"


def test_parse_sse_chunk():
    msg = parse_sse_chunk('{"temp": 21}', event_name="reading", event_id="7")
    assert msg["event"] == "reading"
    assert msg["data"] == {"temp": 21}


@pytest.mark.asyncio
async def test_network_captures_ws_lifecycle_and_sse():
    eng = NetworkEngine(None, lambda: "sid")
    await eng.handle_cdp_event({"method": "Network.webSocketCreated",
                                "params": {"requestId": "w1", "url": "wss://x/socket", "initiator": {}}})
    await eng.handle_cdp_event({"method": "Network.webSocketFrameReceived",
                                "params": {"requestId": "w1", "timestamp": 1,
                                           "response": {"opcode": 1, "payloadData": '42["chat",{"text":"hi"}]'}}})
    await eng.handle_cdp_event({"method": "Network.webSocketFrameSent",
                                "params": {"requestId": "w1", "timestamp": 2,
                                           "response": {"opcode": 1, "payloadData": "2"}}})
    await eng.handle_cdp_event({"method": "Network.eventSourceMessageReceived",
                                "params": {"requestId": "s1", "timestamp": 3, "eventName": "reading",
                                           "eventId": "9", "data": '{"temp":21}'}})
    await eng.handle_cdp_event({"method": "Network.webSocketClosed",
                                "params": {"requestId": "w1", "timestamp": 4}})
    summary = eng.get_stream_summary()
    assert summary["sockets"][0]["state"] == "closed"
    assert summary["sockets"][0]["frames_received"] == 1
    assert summary["heartbeats"] == 1
    assert summary["sse_messages"] == 1
    assert summary["recent_events"][0]["decoded"]["event"] == "chat"
    assert summary["recent_sse"][0]["data"] == {"temp": 21}
    assert "STREAMS" in eng.format_ai_summary()
