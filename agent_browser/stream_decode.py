"""
Semantic decoding for streaming transports (WebSocket + SSE).

Raw frames are rarely what a debugger needs: a Socket.IO event buried in a
``42["chat",{"text":...}]`` packet, or an SSE stream, should surface as a
named event with a preview. Heartbeats are classified so settle/search can
ignore them without losing them.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

_SOCKETIO_TYPES = {
    "0": "connect", "1": "disconnect", "2": "ping", "3": "pong",
    "4": "message", "5": "upgrade", "6": "noop",
}


def decode_ws_payload(payload: Optional[str], opcode: Optional[int] = None) -> Dict[str, Any]:
    """Classify one WebSocket payload (CDP opcode: 1 text, 2 binary, 8-10 control)."""
    if payload is None or payload == "":
        return {"kind": "empty", "heartbeat": False, "preview": ""}
    text = str(payload)
    if opcode == 2:
        return {"kind": "binary", "heartbeat": False, "preview": f"[binary {len(text)} chars, base64]"}
    if text in ("2", "3", "ping", "pong"):
        return {"kind": "heartbeat", "heartbeat": True, "preview": text, "event": "ping" if text in ("2", "ping") else "pong"}

    # Socket.IO / Engine.IO packet: leading digit is the packet type.
    if text[0].isdigit() and text[0] in _SOCKETIO_TYPES:
        ptype = _SOCKETIO_TYPES[text[0]]
        if ptype in ("ping", "pong"):
            return {"kind": "socketio", "heartbeat": True, "preview": text[:120], "event": ptype}
        if text[0] == "4" and len(text) > 1 and text[1] == "2":
            rest = text[2:]
            # Optional namespace segment: /name, before the JSON array.
            if rest.startswith("/"):
                comma = rest.find(",")
                if comma > 0:
                    rest = rest[comma + 1:]
            try:
                arr = json.loads(rest)
                if isinstance(arr, list) and arr:
                    return {
                        "kind": "socketio", "heartbeat": False,
                        "event": str(arr[0]),
                        "data": arr[1] if len(arr) > 1 else None,
                        "preview": rest[:200],
                    }
            except Exception:
                pass
        return {"kind": "socketio", "heartbeat": False, "event": ptype, "preview": text[:200]}

    try:
        parsed = json.loads(text)
        return {"kind": "json", "heartbeat": False, "data": parsed, "preview": text[:200]}
    except Exception:
        return {"kind": "text", "heartbeat": False, "preview": text[:200]}


def parse_sse_chunk(data: Optional[str], event_name: Optional[str] = None,
                    event_id: Optional[str] = None) -> Dict[str, Any]:
    """Normalize one CDP eventSourceMessageReceived payload."""
    text = data or ""
    parsed: Any = text
    try:
        parsed = json.loads(text)
    except Exception:
        pass
    return {
        "event": event_name or "message",
        "id": event_id,
        "data": parsed,
        "preview": text[:200],
    }
