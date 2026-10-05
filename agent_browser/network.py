import time
import json
import base64
import re
import difflib
import requests as http_client
from typing import Dict, Any, List, Optional, Tuple, Callable


class InterceptRule:
    """
    Traffic interception and tampering rule.
    """
    def __init__(
        self,
        rule_id: str,
        url_pattern: str,
        resource_type: Optional[str] = None,
        action: str = "continue",  # 'continue', 'modify_request', 'fulfill', 'fail'
        mock_status: int = 200,
        mock_headers: Optional[Dict[str, str]] = None,
        mock_body: Optional[str] = None,
        modify_headers: Optional[Dict[str, str]] = None,
        modify_body: Optional[str] = None,
        fail_reason: str = "Failed"
    ):
        self.rule_id = rule_id
        self.url_pattern = url_pattern
        self.regex = re.compile(url_pattern) if url_pattern else None
        self.resource_type = resource_type
        self.action = action
        self.mock_status = mock_status
        self.mock_headers = mock_headers or {"Content-Type": "application/json"}
        self.mock_body = mock_body
        self.modify_headers = modify_headers or {}
        self.modify_body = modify_body
        self.fail_reason = fail_reason
        self.hits = 0

    def matches(self, url: str, resource_type: Optional[str] = None) -> bool:
        if self.resource_type and resource_type and self.resource_type.lower() != resource_type.lower():
            return False
        if self.regex:
            return bool(self.regex.search(url))
        return True


class NetworkEngine:
    """
    Agent-Native HTTP/HTTPS & WebSocket Traffic Engine.
    Handles real-time packet recording, search, interception, tampering, and replay.
    """
    def __init__(self, cdp_conn, active_session_id_getter):
        self.cdp = cdp_conn
        self.get_session_id = active_session_id_getter
        self.requests: Dict[str, Dict[str, Any]] = {}  # requestId -> entry
        self.traffic_order: List[str] = []
        self.ws_frames: List[Dict[str, Any]] = []
        self.intercept_rules: Dict[str, InterceptRule] = {}
        self.interception_enabled = False
        self._max_history = 5000

    async def enable_network(self, max_buffer_size: int = 50_000_000):
        sid = self.get_session_id()
        await self.cdp.send("Network.enable", {
            "maxTotalBufferSize": max_buffer_size,
            "maxResourceBufferSize": 25_000_000
        }, session_id=sid)

    async def enable_interception(self):
        sid = self.get_session_id()
        # CDP Fetch.enable requires wildcard matching; using '*' allows python-side regex/filter matching
        await self.cdp.send("Fetch.enable", {
            "patterns": [{"urlPattern": "*", "requestStage": "Request"}]
        }, session_id=sid)
        self.interception_enabled = True

    async def disable_interception(self):
        sid = self.get_session_id()
        try:
            await self.cdp.send("Fetch.disable", {}, session_id=sid)
        except Exception:
            pass
        self.interception_enabled = False

    def add_rule(self, rule: InterceptRule):
        self.intercept_rules[rule.rule_id] = rule

    def remove_rule(self, rule_id: str):
        self.intercept_rules.pop(rule_id, None)

    def clear_rules(self):
        self.intercept_rules.clear()

    async def handle_cdp_event(self, data: Dict[str, Any]):
        method = data.get("method", "")
        params = data.get("params", {})
        session_id = data.get("sessionId")
        target_sid = session_id or self.get_session_id()

        # 1. Request will be sent
        if method == "Network.requestWillBeSent":
            req_id = params.get("requestId")
            req = params.get("request", {})
            wall_time = params.get("wallTime", time.time())
            initiator = params.get("initiator", {})

            entry = {
                "id": req_id,
                "url": req.get("url"),
                "method": req.get("method"),
                "headers": req.get("headers", {}),
                "has_post_data": req.get("hasPostData", False),
                "request_body": req.get("postData", ""),
                "resource_type": params.get("type", "Other"),
                "initiator": initiator,
                "timestamp": wall_time,
                "status": None,
                "status_text": None,
                "response_headers": {},
                "response_body": None,
                "mime_type": None,
                "duration_ms": None,
                "request_cookies": [],
                "request_cookie_header": None,
                "_start_time": params.get("timestamp")
            }
            self.requests[req_id] = entry
            self.traffic_order.append(req_id)
            if len(self.traffic_order) > self._max_history:
                old_id = self.traffic_order.pop(0)
                self.requests.pop(old_id, None)

        # 1.1 Request extra info (contains wire headers including Cookie header and associatedCookies)
        elif method == "Network.requestWillBeSentExtraInfo":
            req_id = params.get("requestId")
            if req_id in self.requests:
                entry = self.requests[req_id]
                wire_headers = params.get("headers", {})
                entry["headers"].update(wire_headers)
                entry["request_cookies"] = params.get("associatedCookies", [])
                for hk, hv in wire_headers.items():
                    if hk.lower() == "cookie":
                        entry["request_cookie_header"] = hv
                        break

        # 2. Response received
        elif method == "Network.responseReceived":
            req_id = params.get("requestId")
            resp = params.get("response", {})
            if req_id in self.requests:
                entry = self.requests[req_id]
                entry["status"] = resp.get("status")
                entry["status_text"] = resp.get("statusText")
                entry["response_headers"] = resp.get("headers", {})
                entry["mime_type"] = resp.get("mimeType")
                entry["remote_ip"] = resp.get("remoteIPAddress")
                entry["remote_port"] = resp.get("remotePort")

        # 2.1 Response extra info (contains wire headers including Set-Cookie)
        elif method == "Network.responseReceivedExtraInfo":
            req_id = params.get("requestId")
            if req_id in self.requests:
                entry = self.requests[req_id]
                extra_headers = params.get("headers", {})
                entry["response_headers"].update(extra_headers)

        # 3. Loading finished -> fetch body
        elif method == "Network.loadingFinished":
            req_id = params.get("requestId")
            if req_id in self.requests:
                entry = self.requests[req_id]
                finish_ts = params.get("timestamp")
                if entry.get("_start_time") and finish_ts:
                    entry["duration_ms"] = round((finish_ts - entry["_start_time"]) * 1000, 2)
                if target_sid:
                    import asyncio
                    asyncio.create_task(self._fetch_body_safe(req_id, target_sid))

        # 4. Loading failed
        elif method == "Network.loadingFailed":
            req_id = params.get("requestId")
            if req_id in self.requests:
                entry = self.requests[req_id]
                entry["status"] = "FAILED"
                entry["error_text"] = params.get("errorText")
                entry["canceled"] = params.get("canceled", False)

        # 5. WebSocket frames
        elif method in ("Network.webSocketFrameReceived", "Network.webSocketFrameSent"):
            resp = params.get("response", {})
            self.ws_frames.append({
                "type": "sent" if method.endswith("Sent") else "received",
                "requestId": params.get("requestId"),
                "timestamp": params.get("timestamp"),
                "opcode": resp.get("opcode"),
                "payload": resp.get("payloadData")
            })
            if len(self.ws_frames) > 2000:
                self.ws_frames.pop(0)

        # 6. Fetch / Intercepted Request
        elif method == "Fetch.requestPaused":
            fetch_id = params.get("requestId")
            req = params.get("request", {})
            url = req.get("url", "")
            rtype = params.get("resourceType")

            matched_rule = None
            for rule in self.intercept_rules.values():
                if rule.matches(url, rtype):
                    matched_rule = rule
                    break

            if matched_rule:
                matched_rule.hits += 1
                if matched_rule.action == "fulfill":
                    body_b64 = base64.b64encode(
                        (matched_rule.mock_body or "").encode("utf-8")
                    ).decode("ascii")
                    headers_list = [{"name": k, "value": v} for k, v in matched_rule.mock_headers.items()]
                    await self.cdp.send("Fetch.fulfillRequest", {
                        "requestId": fetch_id,
                        "responseCode": matched_rule.mock_status,
                        "responseHeaders": headers_list,
                        "body": body_b64
                    }, session_id=target_sid)
                    return

                elif matched_rule.action == "fail":
                    await self.cdp.send("Fetch.failRequest", {
                        "requestId": fetch_id,
                        "errorReason": matched_rule.fail_reason
                    }, session_id=target_sid)
                    return

                elif matched_rule.action == "modify_request":
                    headers_dict = dict(req.get("headers", {}))
                    headers_dict.update(matched_rule.modify_headers)
                    headers_list = [{"name": k, "value": str(v)} for k, v in headers_dict.items()]

                    post_data = matched_rule.modify_body if matched_rule.modify_body is not None else req.get("postData")
                    post_b64 = base64.b64encode(post_data.encode("utf-8")).decode("ascii") if post_data else None

                    kwargs: Dict[str, Any] = {
                        "requestId": fetch_id,
                        "headers": headers_list
                    }
                    if post_b64:
                        kwargs["postData"] = post_b64

                    await self.cdp.send("Fetch.continueRequest", kwargs, session_id=target_sid)
                    return

            # Default: continue unchanged
            await self.cdp.send("Fetch.continueRequest", {"requestId": fetch_id}, session_id=target_sid)

    async def _fetch_body_safe(self, req_id: str, session_id: str):
        entry = self.requests.get(req_id)
        if not entry:
            return
        try:
            res = await self.cdp.send(
                "Network.getResponseBody",
                {"requestId": req_id},
                session_id=session_id,
                timeout=4.0
            )
            body = res.get("body")
            is_b64 = res.get("base64Encoded", False)
            if is_b64 and body:
                try:
                    raw_bytes = base64.b64decode(body)
                    decoded = raw_bytes.decode("utf-8", errors="replace")
                    entry["response_body"] = decoded
                except Exception:
                    entry["response_body"] = f"[Base64 binary: {len(body)} chars]"
            else:
                entry["response_body"] = body
        except Exception:
            pass

    def search_traffic(
        self,
        query: Optional[str] = None,
        status: Optional[int] = None,
        mime: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Search recorded HTTP traffic by text, status code, or MIME type.
        """
        results = []
        q_lower = query.lower() if query else None

        for req_id in reversed(self.traffic_order):
            entry = self.requests.get(req_id)
            if not entry:
                continue

            if status and entry.get("status") != status:
                continue
            if mime and mime.lower() not in str(entry.get("mime_type", "")).lower():
                continue

            if q_lower:
                searchable = f"{entry.get('url', '')} {entry.get('method', '')} {str(entry.get('request_body', ''))} {str(entry.get('response_body', ''))}"
                if q_lower not in searchable.lower():
                    continue

            results.append(entry)
            if len(results) >= limit:
                break

        return results

    def get_request(self, req_id: str) -> Optional[Dict[str, Any]]:
        return self.requests.get(req_id)

    def replay_request(
        self,
        req_id: str,
        mutate_headers: Optional[Dict[str, str]] = None,
        mutate_body: Optional[str] = None,
        mutate_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Replays a previously captured request and computes a side-by-side diff
        between the original baseline response and the replayed response.
        """
        orig = self.requests.get(req_id)
        if not orig:
            raise ValueError(f"Request ID '{req_id}' not found in traffic buffer.")

        target_url = mutate_url or orig["url"]
        method = orig.get("method", "GET").upper()
        headers = dict(orig.get("headers", {}))

        # Filter out browser pseudo-headers like :authority, :method
        headers = {k: v for k, v in headers.items() if not k.startswith(":") and k.lower() != "content-length"}
        if mutate_headers:
            headers.update(mutate_headers)

        body = mutate_body if mutate_body is not None else orig.get("request_body")

        start = time.time()
        try:
            resp = http_client.request(
                method=method,
                url=target_url,
                headers=headers,
                data=body if body else None,
                timeout=15.0,
                verify=False
            )
            elapsed_ms = round((time.time() - start) * 1000, 2)
            replayed_body = resp.text
            replayed_status = resp.status_code
            replayed_headers = dict(resp.headers)
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "original_request": orig
            }

        # Calculate text diff
        orig_body = orig.get("response_body") or ""
        diff_lines = list(difflib.unified_diff(
            orig_body.splitlines(),
            replayed_body.splitlines(),
            fromfile="baseline_response",
            tofile="replayed_response",
            lineterm=""
        ))

        return {
            "success": True,
            "requestId": req_id,
            "url": target_url,
            "method": method,
            "baseline": {
                "status": orig.get("status"),
                "mime_type": orig.get("mime_type"),
                "body_length": len(orig_body),
                "duration_ms": orig.get("duration_ms")
            },
            "replayed": {
                "status": replayed_status,
                "headers": replayed_headers,
                "body_length": len(replayed_body),
                "duration_ms": elapsed_ms,
                "body_preview": replayed_body[:1000]
            },
            "status_changed": (orig.get("status") != replayed_status),
            "diff": "\n".join(diff_lines[:50]) if diff_lines else "[Identical Bodies]"
        }
