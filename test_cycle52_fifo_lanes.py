"""Cycle 52 tests: per-session FIFO lanes with counted spill."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.cdp import CDPConnection


class _WS:
    def __init__(self, msgs):
        self._msgs = list(msgs)

    async def recv(self):
        if self._msgs:
            return self._msgs.pop(0)
        await asyncio.sleep(60)

    async def close(self):
        pass


def _ev(n, session="S1"):
    return ('{"method": "Network.requestWillBeSent", "sessionId": "%s", '
            '"params": {"requestId": "r%d"}}' % (session, n))


async def _pump(conn, msgs, settle=0.2):
    conn.ws = _WS(msgs)
    conn.running = True
    task = asyncio.create_task(conn._listen_loop())
    await asyncio.sleep(settle)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    conn._close_lanes()


@pytest.mark.asyncio
async def test_same_session_events_run_in_arrival_order():
    conn = CDPConnection("ws://127.0.0.1:9/x")
    order = []

    async def handler(data):
        rid = data["params"]["requestId"]
        order.append(("start", rid))
        await asyncio.sleep(0)  # yield point: unordered tasks would interleave
        order.append(("end", rid))

    conn.add_event_handler(handler)
    await _pump(conn, [_ev(1), _ev(2), _ev(3)])
    assert order == [("start", "r1"), ("end", "r1"),
                     ("start", "r2"), ("end", "r2"),
                     ("start", "r3"), ("end", "r3")]
    assert conn.lane_enqueued == 3
    assert conn.lane_spilled == 0


@pytest.mark.asyncio
async def test_stalled_session_does_not_block_other_session():
    conn = CDPConnection("ws://127.0.0.1:9/x")
    gate = asyncio.Event()
    done = []

    async def handler(data):
        if data.get("sessionId") == "SLOW":
            await gate.wait()
            done.append("slow")
        else:
            done.append("fast")

    conn.add_event_handler(handler)
    conn.ws = _WS([_ev(1, "SLOW"), _ev(2, "FAST")])
    conn.running = True
    task = asyncio.create_task(conn._listen_loop())
    await asyncio.sleep(0.15)
    assert done == ["fast"]  # FAST lane progressed while SLOW is parked
    gate.set()
    await asyncio.sleep(0.1)
    task.cancel()
    conn._close_lanes()
    assert done == ["fast", "slow"]


@pytest.mark.asyncio
async def test_lane_overflow_spills_counted_and_critical_never_drops():
    conn = CDPConnection("ws://127.0.0.1:9/x")
    conn._lane_maxsize = 1
    conn._max_handler_tasks = 0  # spill pool sheds non-critical, counts drops
    gate = asyncio.Event()
    ran = []

    async def handler(data):
        method = data.get("method")
        if method == "Fetch.requestPaused":
            ran.append("fetch")
            return
        await gate.wait()
        ran.append(method)

    conn.add_event_handler(handler)
    msgs = [_ev(1), _ev(2), _ev(3),
            '{"method": "Fetch.requestPaused", "sessionId": "S1", "params": {}}']
    await _pump(conn, msgs, settle=0.25)
    # Lane held the parked first event (+1 queued); the rest spilled with a
    # counter, and the critical Fetch pause ran via the never-drop path.
    assert conn.lane_spilled >= 1
    assert "fetch" in ran
    gate.set()
