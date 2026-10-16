"""
Hermetic replay: serve every request from the recording (or a benign stub).

Structural safety: the hermetic fetch handler has **no live path** -- there
is no ``Fetch.continueRequest`` branch while hermetic mode is on, so a write
request can never reach a real backend from a replay/minimization run. It is
either fulfilled from the recorded exchange or answered with a stub.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

from agent_browser.evidence import url_template

GIF_1X1 = "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="

STUBS = {
    "Script": (200, "application/javascript", ""),
    "Stylesheet": (200, "text/css", ""),
    "Image": (200, "image/gif", GIF_1X1),
    "Font": (404, "text/plain", ""),
    "Media": (404, "text/plain", ""),
    # A 204 Document response is a navigation no-op (the browser stays on the
    # current page), which would let a replay cheat on stale DOM. A blank 200
    # document actually replaces the page.
    "Document": (200, "text/html", "<!DOCTYPE html><html><head><title>stub</title></head><body></body></html>"),
}
DEFAULT_STUB = (204, None, "")

_DROP_RESPONSE_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


class ExchangeIndex:
    """
    Sequence-aware matcher: (method, url_template) -> recorded exchanges in
    recorded order. The Nth matching request gets the Nth recorded response;
    when the bucket is exhausted the last exchange repeats (polling), and an
    exchange is only served when its uid is in the keep-set (minimization).
    """

    def __init__(self, exchanges: List[Dict[str, Any]], keep: Optional[set] = None):
        self._buckets: Dict[tuple, List[Dict[str, Any]]] = {}
        self._last: Dict[tuple, Dict[str, Any]] = {}
        self.keep = keep  # None = keep all
        self.requested_uids: set = set()
        for i, ex in enumerate(exchanges or []):
            ex = dict(ex)
            ex.setdefault("uid", f"x:{i}")
            key = self.key(ex.get("method") or "GET", ex.get("url") or "")
            self._buckets.setdefault(key, []).append(ex)

    @staticmethod
    def key(method: str, url: str) -> tuple:
        return ((method or "GET").upper(), url_template(url, keep_query_values=True))

    def take(self, method: str, url: str) -> Optional[Dict[str, Any]]:
        key = self.key(method, url)
        bucket = self._buckets.get(key)
        ex = bucket.pop(0) if bucket else self._last.get(key)
        if ex is None:
            return None
        self._last[key] = ex
        if self.keep is not None and ex.get("uid") not in self.keep:
            return None
        if ex.get("servable") is False:
            return None
        self.requested_uids.add(ex.get("uid"))
        return ex


def exchange_from_entry(entry: Dict[str, Any], uid: Optional[str] = None) -> Dict[str, Any]:
    """Normalize a stored network entry (or HAR entry) into an exchange."""
    body = entry.get("response_body") or ""
    body_b64 = entry.get("body_b64")
    if body_b64 is None:
        body_b64 = base64.b64encode(str(body).encode("utf-8")).decode("ascii") if body else ""
    headers = []
    for name, value in (entry.get("response_headers") or {}).items():
        if str(name).lower() in _DROP_RESPONSE_HEADERS:
            continue
        headers.append({"name": str(name), "value": str(value)})
    return {
        "uid": uid or f"x:{entry.get('id')}",
        "method": (entry.get("method") or "GET").upper(),
        "url": entry.get("url") or "",
        "status": entry.get("status") if isinstance(entry.get("status"), int) else 200,
        "resp_headers": headers,
        "body_b64": body_b64,
        "servable": not entry.get("body_filtered"),
        "resource_type": entry.get("resource_type"),
    }


def exchange_prior(exchange: Dict[str, Any], target_url: Optional[str] = None) -> float:
    """
    Evidence prior P(essential) for minimization ordering: the failing/target
    exchange is almost certainly essential; same-origin XHR/Fetch and scripts
    are plausible; assets and background pings are almost certainly noise.
    """
    url = exchange.get("url") or ""
    status = exchange.get("status")
    rtype = (exchange.get("resource_type") or "").lower()
    if target_url and url_template(url) == url_template(target_url):
        return 0.95
    if url.rstrip("/").endswith("favicon.ico") or rtype in ("image", "font", "media"):
        return 0.02
    if isinstance(status, int) and status >= 400:
        return 0.8
    if "background-ping" in url or "heartbeat" in url or "/ping" in url:
        return 0.03
    if rtype in ("xhr", "fetch"):
        return 0.25
    if rtype == "script":
        return 0.3
    if rtype == "document":
        return 0.9  # the page itself is normally pinned/essential
    if rtype in ("image", "font", "stylesheet", "media"):
        return 0.02
    return 0.1


def build_units(exchanges: List[Dict[str, Any]], target_url: Optional[str] = None) -> List[Dict[str, Any]]:
    """Minimizer units (dicts with uid + prior) from recorded exchanges."""
    units = []
    for ex in exchanges or []:
        units.append({
            "uid": ex.get("uid"),
            "url": ex.get("url"),
            "method": ex.get("method"),
            "prior": exchange_prior(ex, target_url),
        })
    return units


def stub_for(resource_type: Optional[str]) -> Dict[str, Any]:
    status, ctype, body = STUBS.get(resource_type or "", DEFAULT_STUB)
    headers = [{"name": "Content-Type", "value": ctype}] if ctype else []
    is_b64 = (resource_type == "Image")
    return {
        "status": status,
        "resp_headers": headers,
        "body_b64": body if is_b64 else base64.b64encode(str(body).encode()).decode("ascii"),
        "stub": True,
    }
