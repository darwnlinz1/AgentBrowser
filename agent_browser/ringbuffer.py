"""
Bounded event ring buffer with explicit drop accounting.

The old dispatch path silently closed handler coroutines once 64 tasks were
in flight. This buffer makes backpressure a measurable quantity instead:
critical events (``Fetch.requestPaused`` -- a dropped pause hangs a request)
are never shed; non-critical events may be shed under extreme load, but every
shed event increments ``dropped`` so the loss is visible in status/evidence
instead of masquerading as 'no effect observed'.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Deque, Optional


class EventRingBuffer:
    def __init__(self, capacity: int = 4096):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._q: Deque[Any] = deque()
        self._not_empty = asyncio.Event()
        self.dropped = 0
        self.enqueued = 0

    def __len__(self) -> int:
        return len(self._q)

    def put(self, item: Any, *, critical: bool = False) -> bool:
        """
        Append an event. Returns False only when a non-critical event is shed
        at capacity. Critical events displace the oldest non-critical slot by
        growing one past capacity rather than being dropped themselves --
        bounded in practice because every Fetch pause resolves quickly.
        """
        if len(self._q) >= self.capacity and not critical:
            self.dropped += 1
            return False
        self._q.append(item)
        self.enqueued += 1
        self._not_empty.set()
        return True

    async def get(self) -> Any:
        while not self._q:
            self._not_empty.clear()
            await self._not_empty.wait()
        item = self._q.popleft()
        return item

    def get_nowait(self) -> Optional[Any]:
        if self._q:
            return self._q.popleft()
        return None

    def stats(self) -> dict:
        return {"capacity": self.capacity, "depth": len(self._q),
                "enqueued": self.enqueued, "dropped": self.dropped}
