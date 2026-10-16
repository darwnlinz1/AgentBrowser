"""
Flight-recorder journal: every CDP event, sequence-stamped, on disk.

Registered as a *synchronous* CDP subscriber, so recording cannot be shed by
the async handler cap. Secrets are redacted at write time (the journal is an
artifact that may be shared for debugging; raw Cookie/Authorization values
must never land in it). Bounded: when the file passes ``max_bytes`` the
recorder stops and records that fact instead of growing without limit.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Optional

from agent_browser.evidence import redact_mapping

_SECRET_KEYS = {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key"}


def _redact(obj: Any, depth: int = 0) -> Any:
    if depth > 8:
        return obj
    if isinstance(obj, dict):
        if any(str(k).lower() in ("headers",) for k in obj.keys()):
            obj = redact_mapping(obj) if all(isinstance(v, str) for v in obj.values()) else obj
        out = {}
        for k, v in obj.items():
            if str(k).lower() in _SECRET_KEYS and isinstance(v, str):
                out[k] = "[redacted]"
            elif str(k).lower() == "headers" and isinstance(v, dict):
                out[k] = redact_mapping(v)
            else:
                out[k] = _redact(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [_redact(v, depth + 1) for v in obj[:50]]
    return obj


class JournalRecorder:
    def __init__(self, path: str, max_bytes: int = 64 * 1024 * 1024):
        self.path = path
        self.max_bytes = max_bytes
        self.seq = 0
        self.dropped = 0
        self._bytes = 0
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")

    def on_event(self, data: Dict[str, Any]) -> None:
        """Sync CDP subscriber: stamp, redact, append one JSONL line."""
        with self._lock:
            if self._fh is None:
                self.dropped += 1
                return
            self.seq += 1
            record = {
                "seq": self.seq,
                "t_recv": time.time(),
                "sessionId": data.get("sessionId"),
                "method": data.get("method"),
                "params": _redact(data.get("params") or {}),
            }
            line = json.dumps(record, ensure_ascii=False) + "\n"
            self._bytes += len(line.encode("utf-8"))
            if self._bytes > self.max_bytes:
                self._fh.write(json.dumps({"seq": self.seq, "method": "__journal_truncated__",
                                           "params": {"reason": "max_bytes"}}) + "\n")
                self._fh.flush()
                self._fh.close()
                self._fh = None
                self.dropped += 1
                return
            self._fh.write(line)

    def flush(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.flush()
                self._fh.close()
                self._fh = None

    def stats(self) -> Dict[str, Any]:
        return {"path": self.path, "seq": self.seq, "bytes": self._bytes, "dropped": self.dropped}


def journal_path_from_env() -> Optional[str]:
    """Opt-in via AGENTBROWSER_JOURNAL=<path> (disabled by default)."""
    value = os.environ.get("AGENTBROWSER_JOURNAL")
    return value or None


def load_journal(path: str) -> list:
    """Load a journal JSONL file into event records (in seq order)."""
    events = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    events.sort(key=lambda e: e.get("seq", 0))
    return events


def build_exchanges(events: list) -> list:
    """
    Pair journal network events into exchanges (request + response status).

    Bodies are not CDP events, so journal-only exchanges are marked
    ``servable: False`` (stub-only) unless the caller merges bodies from the
    network buffer / HAR export. Request ids are journal-scoped.
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    order: list = []
    for ev in events:
        method = ev.get("method")
        params = ev.get("params") or {}
        if method == "Network.requestWillBeSent":
            rid = params.get("requestId")
            if not rid or rid in by_id:
                continue
            req = params.get("request") or {}
            ex = {
                "uid": f"x:{rid}",
                "method": (req.get("method") or "GET").upper(),
                "url": req.get("url") or "",
                "status": None,
                "resp_headers": [],
                "body_b64": "",
                "servable": False,
                "resource_type": params.get("type"),
                "seq": ev.get("seq"),
            }
            by_id[rid] = ex
            order.append(ex)
        elif method == "Network.responseReceived":
            ex = by_id.get(params.get("requestId"))
            if ex is not None:
                resp = params.get("response") or {}
                ex["status"] = resp.get("status")
                ex["resp_headers"] = [
                    {"name": k, "value": str(v)} for k, v in (resp.get("headers") or {}).items()
                ]
    return order
