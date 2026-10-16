"""
Deterministic quiescence predictor (zero-sleep settling).

Replaces a fixed ``asyncio.sleep(settle_timeout)`` with a state check over the
vectors that actually signal page activity:

1. in-flight network (``requestWillBeSent`` minus ``loadingFinished/Failed``),
   excluding long-lived transports (WebSocket, EventSource/SSE, long-poll
   heartbeats) so persistent connections never block settling;
2. DOM mutation activity (a quiet window with no mutations);
3. lifecycle/navigation state supplied by the caller.

The tracker is pure (monotonic timestamps supplied by the caller) so it is
unit-testable without a browser. ``wait_for_quiescence`` is the async driver.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, Optional, Set

LONG_LIVED_RESOURCE_TYPES = {"websocket", "eventsource", "text/event-stream"}
LONG_LIVED_URL_HINTS = ("/heartbeat", "/ping?", "transport=polling")


def is_long_lived(resource_type: Optional[str], url: Optional[str]) -> bool:
    rtype = (resource_type or "").lower()
    if rtype in LONG_LIVED_RESOURCE_TYPES:
        return True
    u = (url or "").lower()
    return ("event-stream" in u) or u.endswith("/events") or "/sse" in u


class QuiescenceTracker:
    def __init__(self, quiet_window_s: float = 0.3, floor_s: float = 0.0):
        self.quiet_window_s = quiet_window_s
        self.in_flight: Set[str] = set()
        self.long_lived: Set[str] = set()
        # Quiet is measured from creation (the action), never from the epoch:
        # a fresh tracker must not read as "quiet for ages" on its first poll,
        # or a debounced/delayed effect would be missed (false-early settle).
        self.last_activity: float = time.monotonic()
        # Action floor: settle may not declare quiet before this point, even
        # with zero in-flight requests. Typing needs it -- a debounced handler
        # (100-300ms) has not even fired when the keystrokes land, so the
        # network ledger is momentarily empty (research: type floor ~350ms).
        # Anchored lazily at the first is_quiet() clock reading so synthetic
        # clocks in tests are honoured.
        self._floor_s: float = max(0.0, floor_s)
        self.floor_until: float = 0.0
        self.saw_activity: bool = False
        self.lifecycle: Optional[str] = None

    def note_request_start(self, request_id: str, resource_type: Optional[str] = None,
                           url: Optional[str] = None, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        if is_long_lived(resource_type, url):
            self.long_lived.add(request_id)
            return
        self.in_flight.add(request_id)
        self.last_activity = now
        self.saw_activity = True

    def note_request_end(self, request_id: str, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        self.in_flight.discard(request_id)
        self.long_lived.discard(request_id)
        self.last_activity = now

    def note_mutation(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        self.last_activity = now
        self.saw_activity = True

    def note_lifecycle(self, name: str) -> None:
        self.lifecycle = name

    def is_quiet(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        if self.in_flight:
            return False
        if self._floor_s:
            if not self.floor_until:
                self.floor_until = now + self._floor_s
            if now < self.floor_until:
                return False
        return (now - self.last_activity) >= self.quiet_window_s

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.monotonic() if now is None else now
        return {
            "in_flight": sorted(self.in_flight),
            "long_lived_ignored": sorted(self.long_lived),
            "quiet_for_ms": round(max(0.0, (now - self.last_activity)) * 1000, 1),
            "lifecycle": self.lifecycle,
        }


class NetworkActivityMonitor:
    """
    Synchronous CDP subscriber maintaining the in-flight network ledger.

    Registered via ``CDPConnection.add_sync_handler`` it runs inline in the
    listener, so its bookkeeping cannot be shed by the async handler-task
    cap. O(1) per event, never awaits. WebSocket/EventSource/long-poll
    transports are classified non-blocking via ``is_long_lived``.
    """

    # Only these CDP methods mutate the ledger; declaring them lets the
    # listener skip full params parsing for unrelated event traffic.
    CDP_METHODS = frozenset({
        "Network.requestWillBeSent", "Network.loadingFinished",
        "Network.loadingFailed", "Runtime.exceptionThrown",
    })

    def __init__(self):
        self.in_flight: Set[str] = set()
        self.long_lived: Set[str] = set()
        self.activity_seq: int = 0
        self.exceptions: int = 0
        self.last_activity: float = time.monotonic()

    def on_event(self, data: Dict[str, Any]) -> None:
        method = data.get("method")
        params = data.get("params") or {}
        now = time.monotonic()
        if method == "Network.requestWillBeSent":
            rid = params.get("requestId")
            if not rid:
                return
            req = params.get("request") or {}
            if is_long_lived(params.get("type"), req.get("url")):
                self.long_lived.add(rid)
                return
            self.in_flight.add(rid)
            self.activity_seq += 1
            self.last_activity = now
        elif method in ("Network.loadingFinished", "Network.loadingFailed"):
            rid = params.get("requestId")
            was = rid in self.in_flight
            self.in_flight.discard(rid)
            self.long_lived.discard(rid)
            if was:
                self.activity_seq += 1
                self.last_activity = now
        elif method == "Runtime.exceptionThrown":
            self.exceptions += 1
            self.activity_seq += 1
            self.last_activity = now

    def reset_window(self) -> None:
        """Start a fresh observation window (in-flight state is kept)."""
        self.activity_seq = 0
        self.exceptions = 0
        self.last_activity = time.monotonic()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "in_flight": sorted(self.in_flight),
            "long_lived": sorted(self.long_lived),
            "activity_seq": self.activity_seq,
            "exceptions": self.exceptions,
            "last_activity": self.last_activity,
        }


async def wait_for_quiescence(
    tracker: QuiescenceTracker,
    timeout_s: float = 2.0,
    poll: Optional[Callable[[], Any]] = None,
    poll_interval_s: float = 0.02,
) -> Dict[str, Any]:
    """
    Wait until the tracker is quiet, or the hard cap fires.

    ``poll`` is an optional async callable invoked each tick so the caller can
    feed fresh mutation/network counts into the tracker. The hard cap is the
    deadlock invariant: infinite polling or a persistent socket can never hang
    the caller past ``timeout_s``.
    """
    start = time.monotonic()
    # Give the action one tick to register activity before declaring quiet.
    await asyncio.sleep(poll_interval_s)
    while True:
        now = time.monotonic()
        elapsed = now - start
        if poll is not None:
            result = poll()
            if asyncio.iscoroutine(result):
                await result
        if tracker.is_quiet(now):
            return {
                "settled": True,
                "settle_reason": "quiescent",
                "elapsed_ms": round(elapsed * 1000, 1),
                "residual_requests": sorted(tracker.in_flight),
                **tracker.snapshot(now),
            }
        if elapsed >= timeout_s:
            return {
                "settled": False,
                "settle_reason": "timeout",
                "elapsed_ms": round(elapsed * 1000, 1),
                "residual_requests": sorted(tracker.in_flight),
                **tracker.snapshot(now),
            }
        await asyncio.sleep(poll_interval_s)
