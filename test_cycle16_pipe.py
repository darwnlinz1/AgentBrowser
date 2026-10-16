"""Cycle 16 tests: PipeCDPClient framing/routing over in-memory pipes."""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.transport import PipeCDPClient


@pytest.mark.asyncio
async def test_pipe_client_request_reply_and_events():
    # pipe_a: test -> client (client reads a_r, test writes a_w)
    a_r, a_w = os.pipe()
    # pipe_b: client -> test (client writes b_w, test reads b_r)
    b_r, b_w = os.pipe()
    client = PipeCDPClient(write_fd=b_w, read_fd=a_r)
    await client.connect()

    events = []
    client.add_sync_handler(lambda data: events.append(data.get("method")))

    send_task = asyncio.create_task(client.send("Browser.getVersion"))
    # Read the request frame the client wrote.
    await asyncio.sleep(0.05)
    raw = os.read(b_r, 65536)
    frame, _, _ = raw.partition(b"\x00")
    req = json.loads(frame)
    assert req["method"] == "Browser.getVersion"

    # Push an event + the reply back.
    os.write(a_w, (json.dumps({"method": "Network.requestWillBeSent", "params": {}}) + "\0").encode())
    os.write(a_w, (json.dumps({"id": req["id"], "result": {"product": "Chrome/test"}}) + "\0").encode())
    result = await asyncio.wait_for(send_task, timeout=3)
    assert result["product"] == "Chrome/test"
    assert events == ["Network.requestWillBeSent"]
    await client.disconnect()
    os.close(b_r)
    os.close(a_w)
