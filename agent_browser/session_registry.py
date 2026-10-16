"""
Multi-session registry: ``session_id -> controller``.

Replaces the single-global-controller model so several isolated browser
sessions (own profile, own ephemeral DevTools port) can coexist -- the
prerequisite for parallel agents, A/B interventions, and MCP page routing.
The historical default session keeps working: existing routes resolve
``session_id=None`` to the default entry.
"""

from __future__ import annotations

import socket
import uuid
from typing import Any, Dict, List, Optional


def find_free_port() -> int:
    """Ask the OS for an unused loopback port (ephemeral allocation)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SessionRegistry:
    def __init__(self):
        self._items: Dict[str, Any] = {}
        self.default_id: Optional[str] = None

    def register(self, controller: Any, session_id: Optional[str] = None, *, make_default: bool = False) -> str:
        sid = session_id or uuid.uuid4().hex[:12]
        self._items[sid] = controller
        if make_default or self.default_id is None:
            self.default_id = sid
        return sid

    def get(self, session_id: Optional[str] = None) -> Optional[Any]:
        if session_id:
            return self._items.get(session_id)
        if self.default_id:
            return self._items.get(self.default_id)
        return None

    def require(self, session_id: Optional[str] = None) -> Any:
        ctrl = self.get(session_id)
        if ctrl is None or not getattr(ctrl, "_running", False):
            raise LookupError("Browser session not active. Call /session/start first.")
        return ctrl

    def remove(self, session_id: str) -> Optional[Any]:
        ctrl = self._items.pop(session_id, None)
        if self.default_id == session_id:
            self.default_id = next(iter(self._items), None)
        return ctrl

    def ids(self) -> List[str]:
        return list(self._items)

    def list_summaries(self) -> List[Dict[str, Any]]:
        out = []
        for sid, ctrl in self._items.items():
            out.append({
                "session_id": sid,
                "is_default": sid == self.default_id,
                "active": bool(getattr(ctrl, "_running", False)),
                "port": getattr(ctrl, "port", None),
                "profile_name": getattr(ctrl, "profile_name", None),
                "target_id": getattr(ctrl, "active_target_id", None),
            })
        return out

    async def stop_all(self) -> None:
        for ctrl in list(self._items.values()):
            try:
                await ctrl.stop()
            except Exception:
                pass
        self._items.clear()
        self.default_id = None
