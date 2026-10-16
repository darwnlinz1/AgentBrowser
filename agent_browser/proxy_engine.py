import os
import sys
import time
import json
import asyncio
import threading
from typing import Dict, Any, List, Optional, Callable


class ProxyTrafficHub:
    """
    Thread-safe in-memory traffic hub holding normalized requests/responses
    intercepted across all browsers (Firefox, Safari, Chromium, curl, OpenBullet).
    """
    def __init__(self, max_history: int = 5000):
        self.max_history = max_history
        self._lock = threading.Lock()
        self.flows: Dict[str, Dict[str, Any]] = {}
        self.flow_order: List[str] = []

    def record_flow(self, flow_data: Dict[str, Any]):
        with self._lock:
            fid = flow_data["id"]
            self.flows[fid] = flow_data
            self.flow_order.append(fid)
            if len(self.flow_order) > self.max_history:
                old_id = self.flow_order.pop(0)
                self.flows.pop(old_id, None)

    def update_response(self, flow_id: str, resp_data: Dict[str, Any]):
        with self._lock:
            if flow_id in self.flows:
                self.flows[flow_id].update(resp_data)

    def get_flows(self, query: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            results = []
            q_lower = query.lower() if query else None
            for fid in reversed(self.flow_order):
                item = self.flows.get(fid)
                if not item:
                    continue
                if q_lower:
                    searchable = f"{item.get('url', '')} {item.get('method', '')} {str(item.get('request_body', ''))} {str(item.get('response_body', ''))}"
                    if q_lower not in searchable.lower():
                        continue
                results.append(item)
                if len(results) >= limit:
                    break
            return results

    def get_flow(self, flow_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.flows.get(flow_id)

    def clear(self):
        with self._lock:
            self.flows.clear()
            self.flow_order.clear()


class MitmAddon:
    """
    mitmproxy Event Addon capturing all HTTP/HTTPS requests, wire headers,
    response cookies, and decompressed payloads.
    """
    def __init__(self, hub: ProxyTrafficHub):
        self.hub = hub

    def request(self, flow):
        req = flow.request
        headers = dict(req.headers.items())
        
        # Extract cookies from Cookie header
        cookies_dict = dict(req.cookies.items())

        # Body decode
        body_text = None
        try:
            body_text = req.get_text()
        except Exception:
            if req.raw_content:
                body_text = f"[Binary payload: {len(req.raw_content)} bytes]"

        flow_data = {
            "id": flow.id,
            "engine": "mitmproxy",
            "url": req.url,
            "method": req.method,
            "headers": headers,
            "request_cookies": cookies_dict,
            "request_body": body_text,
            "timestamp": time.time(),
            "status": None,
            "response_headers": {},
            "response_body": None,
            "response_cookies": {},
            "mime_type": None,
            "duration_ms": None,
            "initiator": {"type": "external_client"}
        }
        self.hub.record_flow(flow_data)

    def response(self, flow):
        resp = flow.response
        headers = dict(resp.headers.items())
        
        # Extract Set-Cookie response cookies
        res_cookies = dict(resp.cookies.items())

        body_text = None
        try:
            body_text = resp.get_text()
        except Exception:
            if resp.raw_content:
                body_text = f"[Binary payload: {len(resp.raw_content)} bytes]"

        duration = None
        if hasattr(flow, "timestamp_end") and hasattr(flow, "timestamp_start"):
            if flow.timestamp_end and flow.timestamp_start:
                duration = round((flow.timestamp_end - flow.timestamp_start) * 1000, 2)

        update_data = {
            "status": resp.status_code,
            "status_text": resp.reason,
            "response_headers": headers,
            "response_cookies": res_cookies,
            "response_body": body_text,
            "mime_type": headers.get("content-type") or headers.get("Content-Type"),
            "duration_ms": duration
        }
        self.hub.update_response(flow.id, update_data)


class LocalProxyServer:
    """
    Embedded Async MITM Proxy Server running in background thread.
    Exposes transparent and forward proxy listening on localhost (default 8888).
    Works universally with Firefox, Safari, curl, and OpenBullet.
    """
    def __init__(self, host: str = "127.0.0.1", port: int = 8888, upstream_proxy: Optional[str] = None):
        self.host = host
        self.port = port
        self.upstream_proxy = upstream_proxy
        self.hub = ProxyTrafficHub()
        self.master = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self):
        if self._running:
            return

        def _run():
            from mitmproxy.tools.dump import DumpMaster
            from mitmproxy.options import Options

            opts_kwargs = {
                "listen_host": self.host,
                "listen_port": self.port,
            }
            if self.upstream_proxy:
                opts_kwargs["mode"] = [f"upstream:{self.upstream_proxy}"]

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _main():
                opts = Options(**opts_kwargs)
                self.master = DumpMaster(opts, with_termlog=False, with_dumper=False)
                self.master.addons.add(MitmAddon(self.hub))
                self._running = True
                await self.master.run()

            try:
                loop.run_until_complete(_main())
            except Exception:
                pass
            finally:
                self._running = False

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        # Wait for socket start
        for _ in range(20):
            if self._running:
                break
            time.sleep(0.1)

    def stop(self):
        if self.master:
            try:
                self.master.shutdown()
            except Exception:
                pass
        self._running = False

    def is_running(self) -> bool:
        return self._running
