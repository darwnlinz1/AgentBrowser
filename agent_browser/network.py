import asyncio
import time
import json
import base64
import re
import difflib
import requests as http_client
from typing import Dict, Any, List, Optional, Tuple, Callable

from agent_browser.evidence import redact_mapping


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
        fail_reason: str = "Failed",
        modify_response_headers: Optional[Dict[str, str]] = None,
        modify_response_body: Optional[str] = None,
        delay_ms: int = 0
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
        # Response-stage (Phase 3): rewrite a live response body/headers.
        self.modify_response_headers = modify_response_headers or {}
        self.modify_response_body = modify_response_body
        # Interventional delay (ms) applied before a request continues.
        self.delay_ms = int(delay_ms or 0)
        self.hits = 0
        self.response_hits = 0

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
        self.smart_filter: bool = True
        self._max_history = 5000
        self._pending_extra: Dict[str, Dict[str, Any]] = {}
        # Hermetic replay (cycle 12): when set, Fetch pauses are fulfilled
        # from the recording or stubbed -- there is no live continue path.
        self.hermetic_index = None
        # Bounded body-fetcher pool (cycle 13): body fetches run off the
        # event-handler hot path, capped so a burst cannot pile up tasks.
        self._body_semaphore: Optional[asyncio.Semaphore] = None
        self._body_inflight: set = set()
        self.body_fetch_stats: Dict[str, int] = {"submitted": 0, "completed": 0, "failed": 0, "shed": 0}
        self._max_body_inflight = 256

    def _blank_entry(self, req_id: str) -> Dict[str, Any]:
        return {
            "id": req_id,
            "url": None,
            "method": None,
            "headers": {},
            "has_post_data": False,
            "request_body": "",
            "post_data_complete": True,
            "resource_type": "Other",
            "initiator": {},
            "timestamp": None,
            "status": None,
            "status_text": None,
            "status_from_extra": None,
            "response_headers": {},
            "response_headers_raw": [],
            "response_headers_text": None,
            "response_body": None,
            "mime_type": None,
            "protocol": None,
            "duration_ms": None,
            "request_cookies": [],
            "request_cookie_header": None,
            "encoded_data_length": None,
            "capture_gaps": [],
            "redirect_hops": [],
            "body_error": None,
            "_start_time": None,
        }

    def _note_gap(self, entry: Dict[str, Any], gap: str):
        gaps = entry.setdefault("capture_gaps", [])
        if gap not in gaps:
            gaps.append(gap)

    def _apply_pending_extra(self, req_id: str, entry: Dict[str, Any]):
        extra = self._pending_extra.pop(req_id, None)
        if not extra:
            return
        req_extra = extra.get("request")
        if req_extra:
            self._merge_request_extra(entry, req_extra)
        resp_extra = extra.get("response")
        if resp_extra:
            self._merge_response_extra(entry, resp_extra)

    def _merge_request_extra(self, entry: Dict[str, Any], params: Dict[str, Any]):
        wire_headers = params.get("headers", {}) or {}
        entry.setdefault("headers", {}).update(wire_headers)
        entry["request_cookies"] = params.get("associatedCookies", entry.get("request_cookies") or [])
        for hk, hv in wire_headers.items():
            if hk.lower() == "cookie":
                entry["request_cookie_header"] = hv
                break

    def _merge_response_extra(self, entry: Dict[str, Any], params: Dict[str, Any]):
        extra_headers = params.get("headers", {}) or {}
        entry.setdefault("response_headers", {}).update(extra_headers)
        raw = params.get("headers")
        # CDP extra-info headers may be a dict or a list of name/value pairs.
        # Set-Cookie can repeat, so a dict merge is not enough to count them.
        if isinstance(raw, list):
            entry["response_headers_raw"] = raw
        else:
            pairs = [{"name": k, "value": v} for k, v in extra_headers.items()]
            headers_text = params.get("headersText") or ""
            # A dict collapses repeated Set-Cookie. Recover each line from headersText.
            repeated = []
            for key, value in extra_headers.items():
                if str(key).lower() != "set-cookie" or not isinstance(value, str):
                    continue
                # A dict cannot repeat a header. CDP sometimes joins Set-Cookie
                # with a newline. Split those back into one pair each.
                chunks = [part.strip() for part in value.splitlines() if part.strip()]
                if len(chunks) > 1:
                    repeated.extend({"name": key, "value": part} for part in chunks)
            if headers_text:
                text_pairs = []
                for line in headers_text.splitlines():
                    if ":" not in line:
                        continue
                    name, value = line.split(":", 1)
                    if name.strip().lower() == "set-cookie":
                        text_pairs.append({"name": name.strip(), "value": value.strip()})
                if len(text_pairs) > 1:
                    repeated = text_pairs
            if repeated:
                pairs = [p for p in pairs if p["name"].lower() != "set-cookie"] + repeated
            entry["response_headers_raw"] = pairs
        if params.get("headersText"):
            entry["response_headers_text"] = params.get("headersText")
        if params.get("statusCode") is not None:
            entry["status_from_extra"] = params.get("statusCode")
            entry["status"] = params.get("statusCode")

    def set_smart_filter(self, enabled: bool):
        self.smart_filter = enabled

    def should_filter_asset(self, entry: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Determines whether the response body should be skipped by the smart filter.
        Images (.png, .jpg, .jpeg, .webp, .ico, .gif), fonts (.woff, .woff2, .ttf),
        and media/binary assets are skipped to save RAM and avoid polluting the buffer.
        APIs, Documents, Scripts, JSON, and HTML responses are 100% preserved.
        """
        if not self.smart_filter:
            return False, None

        mime = (entry.get("mime_type") or "").lower().strip()
        rtype = (entry.get("resource_type") or "").strip()
        url = (entry.get("url") or "").lower().split("?")[0].split("#")[0]

        # 1. Unconditionally preserved types: JSON, HTML, Script, XML, plain text
        preserved_mime_tokens = ("json", "javascript", "ecmascript", "html", "xml", "text/plain")
        if any(tok in mime for tok in preserved_mime_tokens):
            return False, None

        # 2. Check by Image extension / mime / resource type
        image_exts = (".png", ".jpg", ".jpeg", ".webp", ".ico", ".gif", ".bmp", ".tiff")
        if any(url.endswith(ext) for ext in image_exts) or mime.startswith("image/") or rtype == "Image":
            return True, "image"

        # 3. Check by Font extension / mime / resource type
        font_exts = (".woff", ".woff2", ".ttf", ".otf", ".eot")
        if any(url.endswith(ext) for ext in font_exts) or mime.startswith("font/") or "font" in mime or rtype == "Font":
            return True, "font"

        # 4. Check by Media (audio/video) extension / mime / resource type
        media_exts = (".mp4", ".mp3", ".webm", ".ogg", ".wav", ".flac", ".avi", ".mov", ".m4a")
        if any(url.endswith(ext) for ext in media_exts) or mime.startswith("video/") or mime.startswith("audio/") or rtype == "Media":
            return True, "media"

        # 5. Check other binary content types
        if mime in ("application/octet-stream", "application/zip", "application/x-gzip", "application/pdf"):
            if rtype not in ("XHR", "Fetch"):
                return True, "binary"

        return False, None

    async def enable_network(self, max_buffer_size: int = 50_000_000, session_id: Optional[str] = None):
        sid = session_id or self.get_session_id()
        try:
            # Durable messages keep response bodies outside the renderer so
            # they survive cross-process navigation -- this is what makes it
            # safe to fetch bodies from a pool instead of inline (experimental
            # on some builds; failure is non-fatal).
            await self.cdp.send("Network.configureDurableMessages", {
                "maxTotalBufferSize": max_buffer_size,
                "maxResourceBufferSize": 25_000_000,
            }, session_id=sid)
        except Exception:
            pass
        await self.cdp.send("Network.enable", {
            "maxTotalBufferSize": max_buffer_size,
            "maxResourceBufferSize": 25_000_000
        }, session_id=sid)

    async def enable_interception(self, include_response: bool = False):
        sid = self.get_session_id()
        # CDP Fetch.enable requires wildcard matching; using '*' allows python-side regex/filter matching
        patterns = [{"urlPattern": "*", "requestStage": "Request"}]
        if include_response:
            # Response stage unlocks match-and-replace on live responses.
            patterns.append({"urlPattern": "*", "requestStage": "Response"})
        await self.cdp.send("Fetch.enable", {
            "patterns": patterns
        }, session_id=sid)
        self.interception_enabled = True

    async def disable_interception(self):
        sid = self.get_session_id()
        try:
            await self.cdp.send("Fetch.disable", {}, session_id=sid)
        except Exception:
            pass
        self.interception_enabled = False

    async def enable_hermetic_replay(self, exchanges: List[Dict[str, Any]], keep: Optional[set] = None):
        """
        Turn on hermetic replay from recorded exchanges (stored network
        entries or pre-normalized exchanges). Every paused request is then
        fulfilled from the recording or stubbed; nothing goes live.
        """
        from agent_browser.replay import ExchangeIndex, exchange_from_entry

        normalized = [
            ex if "resp_headers" in ex else exchange_from_entry(ex)
            for ex in exchanges
        ]
        self.hermetic_index = ExchangeIndex(normalized, keep=keep)
        sid = self.get_session_id()
        await self.cdp.send("Fetch.enable", {
            "patterns": [{"urlPattern": "*", "requestStage": "Request"}]
        }, session_id=sid)
        self.interception_enabled = True
        return {"hermetic": True, "exchanges": len(normalized)}

    async def disable_hermetic_replay(self):
        self.hermetic_index = None
        await self.disable_interception()

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
            redirect = params.get("redirectResponse")

            hops = []
            if redirect and req_id in self.requests:
                prior = self.requests[req_id]
                hop_index = len(prior.get("redirect_hops") or [])
                hop_id = f"{req_id}#redirect-{hop_index}"
                # Freeze the previous request as the hop. Do not copy the
                # follow-up URL that this event is about to install.
                hop = dict(prior)
                hop["id"] = hop_id
                hop["status"] = redirect.get("status")
                hop["status_text"] = redirect.get("statusText")
                hop["response_headers"] = dict(redirect.get("headers") or {})
                hop["mime_type"] = redirect.get("mimeType")
                hop["protocol"] = redirect.get("protocol")
                hop["remote_ip"] = redirect.get("remoteIPAddress")
                hop["remote_port"] = redirect.get("remotePort")
                hop["body_error"] = "redirect_response_has_no_body"
                hop["response_body"] = None
                self._note_gap(hop, "redirect_body_unavailable")
                self.requests[hop_id] = hop
                if req_id in self.traffic_order:
                    at = self.traffic_order.index(req_id)
                    self.traffic_order.insert(at + 1, hop_id)
                else:
                    self.traffic_order.append(hop_id)
                hops = list(prior.get("redirect_hops") or [])
                hops.append(hop_id)

            entry = self.requests.get(req_id) or self._blank_entry(req_id)
            preserved_hops = entry.get("redirect_hops") or []
            if redirect and req_id in self.requests:
                preserved_hops = hops
            entry.update({
                "id": req_id,
                "url": req.get("url"),
                "method": req.get("method"),
                "headers": dict(req.get("headers") or {}),
                "has_post_data": req.get("hasPostData", False),
                "request_body": req.get("postData", "") or "",
                "resource_type": params.get("type", "Other"),
                "initiator": initiator,
                "timestamp": wall_time,
                "status": None,
                "status_text": None,
                "response_body": None,
                "mime_type": None,
                "duration_ms": None,
                "_start_time": params.get("timestamp"),
                "redirect_hops": preserved_hops,
            })
            if req.get("hasPostData") and not req.get("postData"):
                entry["post_data_complete"] = False
                self._note_gap(entry, "post_data_not_inlined")
            else:
                entry["post_data_complete"] = True
            self._apply_pending_extra(req_id, entry)
            self.requests[req_id] = entry
            if req_id not in self.traffic_order:
                self.traffic_order.append(req_id)
            while len(self.traffic_order) > self._max_history:
                old_id = self.traffic_order.pop(0)
                self.requests.pop(old_id, None)

        # 1.1 Request extra info (contains wire headers including Cookie header and associatedCookies)
        elif method == "Network.requestWillBeSentExtraInfo":
            req_id = params.get("requestId")
            if req_id in self.requests:
                self._merge_request_extra(self.requests[req_id], params)
            else:
                bucket = self._pending_extra.setdefault(req_id, {})
                bucket["request"] = params

        # 2. Response received
        elif method == "Network.responseReceived":
            req_id = params.get("requestId")
            resp = params.get("response", {})
            if req_id in self.requests:
                entry = self.requests[req_id]
                # Extra-info status is the wire status (for example 304). Do not
                # let the cached responseReceived status overwrite it.
                if entry.get("status_from_extra") is None:
                    entry["status"] = resp.get("status")
                entry["status_text"] = resp.get("statusText")
                entry["response_headers"] = dict(resp.get("headers") or {})
                entry["mime_type"] = resp.get("mimeType")
                entry["protocol"] = resp.get("protocol")
                entry["remote_ip"] = resp.get("remoteIPAddress")
                entry["remote_port"] = resp.get("remotePort")
                self._apply_pending_extra(req_id, entry)

        # 2.1 Response extra info (contains wire headers including Set-Cookie)
        elif method == "Network.responseReceivedExtraInfo":
            req_id = params.get("requestId")
            if req_id in self.requests:
                self._merge_response_extra(self.requests[req_id], params)
            else:
                bucket = self._pending_extra.setdefault(req_id, {})
                bucket["response"] = params

        # 3. Loading finished -> fetch body
        elif method == "Network.loadingFinished":
            req_id = params.get("requestId")
            if req_id in self.requests:
                entry = self.requests[req_id]
                finish_ts = params.get("timestamp")
                if params.get("encodedDataLength") is not None:
                    entry["encoded_data_length"] = params.get("encodedDataLength")
                if entry.get("_start_time") and finish_ts:
                    entry["duration_ms"] = round((finish_ts - entry["_start_time"]) * 1000, 2)
                should_filter, reason = self.should_filter_asset(entry)
                if should_filter:
                    entry["body_filtered"] = True
                    entry["filter_reason"] = reason
                    entry["response_body"] = f"[SmartFilter: Skipped {reason} body ({entry.get('mime_type') or entry.get('resource_type') or 'asset'})]"
                else:
                    entry["body_filtered"] = False
                    entry["filter_reason"] = None
                    if target_sid:
                        # Fetch from the bounded pool, not inline: an inline
                        # await here holds the event handler (and its task
                        # slot) across a full Chrome round trip. Durable
                        # messages (enabled above) keep the body available.
                        self._submit_body_fetch(req_id, target_sid)

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

            # 6h. Hermetic replay: fulfill from the recording or stub.
            # There is deliberately no continueRequest in this branch, so a
            # replay/minimization run can never reach a live backend.
            if self.hermetic_index is not None and params.get("requestStage") != "Response":
                from agent_browser.replay import stub_for

                ex = self.hermetic_index.take(req.get("method", "GET"), url)
                payload = ex if ex is not None else stub_for(rtype)
                await self.cdp.send("Fetch.fulfillRequest", {
                    "requestId": fetch_id,
                    "responseCode": payload.get("status") or 200,
                    "responseHeaders": payload.get("resp_headers") or [],
                    "body": payload.get("body_b64") or "",
                }, session_id=target_sid)
                return

            # 6a. Response stage: match-and-replace on the live response.
            if params.get("requestStage") == "Response":
                resp_rule = None
                for rule in self.intercept_rules.values():
                    if rule.matches(url, rtype) and (rule.modify_response_body is not None or rule.modify_response_headers):
                        resp_rule = rule
                        break
                if resp_rule is not None:
                    resp_rule.response_hits += 1
                    body_text = ""
                    try:
                        got = await self.cdp.send("Fetch.getResponseBody", {"requestId": fetch_id}, session_id=target_sid)
                        raw = got.get("body") or ""
                        if got.get("base64Encoded"):
                            raw = base64.b64decode(raw).decode("utf-8", errors="replace")
                        body_text = raw
                    except Exception:
                        body_text = ""
                    new_body = resp_rule.modify_response_body if resp_rule.modify_response_body is not None else body_text
                    headers_dict = {}
                    for h in params.get("responseHeaders") or []:
                        headers_dict[h.get("name")] = h.get("value")
                    headers_dict.update(resp_rule.modify_response_headers)
                    await self.cdp.send("Fetch.fulfillRequest", {
                        "requestId": fetch_id,
                        "responseCode": params.get("responseStatusCode") or 200,
                        "responseHeaders": [{"name": k, "value": str(v)} for k, v in headers_dict.items()],
                        "body": base64.b64encode(str(new_body).encode("utf-8")).decode("ascii"),
                    }, session_id=target_sid)
                    return
                # No response rule: continue with the original response. If
                # continueResponse is unavailable, fulfill with the ORIGINAL
                # body+headers (fetched first) -- never a bare status, which
                # would silently strip the response body.
                try:
                    await self.cdp.send("Fetch.continueResponse", {"requestId": fetch_id}, session_id=target_sid)
                except Exception:
                    orig_body = ""
                    try:
                        got = await self.cdp.send("Fetch.getResponseBody", {"requestId": fetch_id}, session_id=target_sid)
                        raw = got.get("body") or ""
                        if got.get("base64Encoded"):
                            raw = base64.b64decode(raw).decode("utf-8", errors="replace")
                        orig_body = raw
                    except Exception:
                        orig_body = ""
                    await self.cdp.send("Fetch.fulfillRequest", {
                        "requestId": fetch_id,
                        "responseCode": params.get("responseStatusCode") or 200,
                        "responseHeaders": params.get("responseHeaders") or [],
                        "body": base64.b64encode(orig_body.encode("utf-8")).decode("ascii"),
                    }, session_id=target_sid)
                return

            matched_rule = None
            for rule in self.intercept_rules.values():
                if rule.matches(url, rtype):
                    matched_rule = rule
                    break

            if matched_rule:
                matched_rule.hits += 1
                if matched_rule.delay_ms:
                    await asyncio.sleep(matched_rule.delay_ms / 1000.0)
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

    async def await_body(self, req_id: str, timeout: float = 2.0, session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Wait until a recorded response body arrives, or the fetch attempt finishes."""
        import asyncio
        deadline = time.time() + timeout
        while time.time() < deadline:
            entry = self.requests.get(req_id)
            if not entry:
                return None
            pending = entry.get("_body_task")
            if pending is not None and not pending.done():
                try:
                    await asyncio.wait_for(asyncio.shield(pending), timeout=max(0.05, deadline - time.time()))
                except Exception:
                    pass
                return self.requests.get(req_id)
            if entry.get("response_body") is not None or entry.get("body_error") or entry.get("body_filtered"):
                return entry
            if entry.get("status") == "FAILED":
                return entry
            await asyncio.sleep(0.05)
        entry = self.requests.get(req_id)
        if entry and entry.get("response_body") is None and not entry.get("body_error"):
            sid = session_id or self.get_session_id()
            if sid:
                await self._fetch_body_safe(req_id, sid)
        return self.requests.get(req_id)

    def _submit_body_fetch(self, req_id: str, session_id: str):
        """Queue a body fetch on the bounded pool; returns immediately."""
        entry = self.requests.get(req_id)
        if not entry or entry.get("body_filtered"):
            return
        pending = entry.get("_body_task")
        if pending is not None and not pending.done():
            return
        if len(self._body_inflight) >= self._max_body_inflight:
            self.body_fetch_stats["shed"] += 1
            self._note_gap(entry, "body_queue_full")
            return
        if self._body_semaphore is None:
            self._body_semaphore = asyncio.Semaphore(6)
        self._body_inflight.add(req_id)
        self.body_fetch_stats["submitted"] += 1
        entry["_body_task"] = asyncio.create_task(self._fetch_body_pooled(req_id, session_id))

    async def _fetch_body_pooled(self, req_id: str, session_id: str):
        try:
            async with self._body_semaphore:
                await self._fetch_body_safe(req_id, session_id)
            self.body_fetch_stats["completed"] += 1
        except Exception:
            self.body_fetch_stats["failed"] += 1
        finally:
            self._body_inflight.discard(req_id)

    async def _fetch_body_safe(self, req_id: str, session_id: str):
        entry = self.requests.get(req_id)
        if not entry or entry.get("body_filtered"):
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
            entry["body_filtered"] = False
        except Exception as exc:
            entry["body_error"] = str(exc)
            self._note_gap(entry, "getResponseBody_failed")

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

    SAFE_REPLAY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    def replay_request(
        self,
        req_id: str,
        mutate_headers: Optional[Dict[str, str]] = None,
        mutate_body: Optional[str] = None,
        mutate_url: Optional[str] = None,
        allow_write: bool = False
    ) -> Dict[str, Any]:
        """
        Replays a previously captured request and computes a side-by-side diff
        between the original baseline response and the replayed response.

        Write methods (POST/PUT/PATCH/DELETE) are refused unless the caller
        explicitly opts in with ``allow_write=True`` -- a captured write must
        never be silently re-sent to a live backend with its original
        Cookie/Authorization headers.
        """
        orig = self.requests.get(req_id)
        if not orig:
            raise ValueError(f"Request ID '{req_id}' not found in traffic buffer.")

        target_url = mutate_url or orig["url"]
        method = orig.get("method", "GET").upper()
        if method not in self.SAFE_REPLAY_METHODS and not allow_write:
            return {
                "success": False,
                "error": f"refusing to replay {method} without explicit allow_write (write requests are not auto-replayed)",
                "replay_kind": "out_of_browser",
                "original_request_id": req_id,
            }
        headers = dict(orig.get("headers", {}))

        # Filter out browser pseudo-headers like :authority, :method
        headers = {k: v for k, v in headers.items() if not k.startswith(":") and k.lower() != "content-length"}
        if mutate_headers:
            headers.update(mutate_headers)

        body = mutate_body if mutate_body is not None else orig.get("request_body")

        limitation = (
            "out-of-browser replay: this is a fresh HTTP client request. "
            "It does not use the page cookie jar, the browser HTTP/2 session, "
            "or the original redirect chain."
        )
        start = time.time()
        try:
            resp = http_client.request(
                method=method,
                url=target_url,
                headers=headers,
                data=body if body else None,
                timeout=15.0,
            )
            elapsed_ms = round((time.time() - start) * 1000, 2)
            replayed_body = resp.text
            replayed_status = resp.status_code
            replayed_headers = dict(resp.headers)
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "replay_kind": "out_of_browser",
                "limitation": limitation,
                "original_request_id": req_id,
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
            "replay_kind": "out_of_browser",
            "limitation": limitation,
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

    @staticmethod
    def _diff_bodies(orig_body: str, replayed_body: str) -> str:
        diff_lines = list(difflib.unified_diff(
            (orig_body or "").splitlines(), (replayed_body or "").splitlines(),
            fromfile="baseline_response", tofile="replayed_response", lineterm="",
        ))
        return "\n".join(diff_lines[:50]) if diff_lines else "[Identical Bodies]"

    async def replay_in_page_fetch(
        self,
        req_id: str,
        mutate_headers: Optional[Dict[str, str]] = None,
        mutate_body: Optional[str] = None,
        mutate_url: Optional[str] = None,
        allow_write: bool = False,
    ) -> Dict[str, Any]:
        """
        Faithful in-browser replay: re-issue the request with ``fetch()`` in the
        page context, so it inherits the page cookie jar, the live HTTP/2
        session, CORS context, and redirect following. Never auto-replays a
        write method against a live backend unless the caller opted in via
        ``mutate_*`` (the caller is the consent point); GET/HEAD are the
        default-safe cases.
        """
        orig = self.requests.get(req_id)
        if not orig:
            raise ValueError(f"Request ID '{req_id}' not found in traffic buffer.")
        sid = self.get_session_id()
        target_url = mutate_url or orig["url"]
        method = (orig.get("method") or "GET").upper()
        if method not in self.SAFE_REPLAY_METHODS and not allow_write:
            return {"success": False,
                    "error": f"refusing to replay {method} in-page without explicit allow_write",
                    "replay_kind": "in_page_fetch", "original_request_id": req_id}
        headers = {k: v for k, v in (orig.get("headers") or {}).items()
                   if not k.startswith(":") and k.lower() not in ("content-length", "cookie", "host")}
        if mutate_headers:
            headers.update(mutate_headers)
        body = mutate_body if mutate_body is not None else orig.get("request_body")
        spec = {"url": target_url, "method": method, "headers": headers,
                "body": body if (body and method not in ("GET", "HEAD")) else None}
        expr = f"""
        (async () => {{
            const spec = {json.dumps(spec)};
            try {{
                const res = await fetch(spec.url, {{
                    method: spec.method, headers: spec.headers,
                    body: spec.body, credentials: 'include', redirect: 'follow'
                }});
                const text = await res.text();
                const hdrs = {{}};
                res.headers.forEach((v, k) => {{ hdrs[k] = v; }});
                return {{ ok: true, status: res.status, headers: hdrs, body: text.slice(0, 20000) }};
            }} catch (e) {{
                return {{ ok: false, error: String(e) }};
            }}
        }})()
        """
        start = time.time()
        res = await self.cdp.send("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
        }, session_id=sid, timeout=20.0)
        elapsed_ms = round((time.time() - start) * 1000, 2)
        val = (res.get("result") or {}).get("value") or {}
        if not val.get("ok"):
            return {"success": False, "error": val.get("error") or "in-page fetch failed",
                    "replay_kind": "in_page_fetch", "original_request_id": req_id}
        replayed_body = val.get("body") or ""
        orig_body = orig.get("response_body") or ""
        return {
            "success": True,
            "replay_kind": "in_page_fetch",
            "limitation": "in-browser replay via page fetch(): shares cookie jar, HTTP/2 session, and redirect chain.",
            "requestId": req_id, "url": target_url, "method": method,
            "baseline": {"status": orig.get("status"), "body_length": len(orig_body), "duration_ms": orig.get("duration_ms")},
            "replayed": {"status": val.get("status"), "headers": val.get("headers") or {},
                         "body_length": len(replayed_body), "duration_ms": elapsed_ms, "body_preview": replayed_body[:1000]},
            "status_changed": (orig.get("status") != val.get("status")),
            "diff": self._diff_bodies(orig_body, replayed_body),
        }

    async def replay_xhr(self, req_id: str) -> Dict[str, Any]:
        """Ask Chrome to re-issue an XHR exactly as recorded (``Network.replayXHR``)."""
        if req_id not in self.requests:
            raise ValueError(f"Request ID '{req_id}' not found in traffic buffer.")
        sid = self.get_session_id()
        await self.cdp.send("Network.replayXHR", {"requestId": req_id}, session_id=sid)
        return {"success": True, "replay_kind": "in_browser_xhr",
                "limitation": "Chrome re-issues the XHR in-page; observe the new request in traffic.",
                "requestId": req_id}

    def export_har(self, include_secrets: bool = False, limit: int = 500) -> Dict[str, Any]:
        """HAR 1.2 export of the capture buffer (redacted unless opted in)."""
        from agent_browser.evidence import redact_entry as _redact
        entries = []
        for req_id in self.traffic_order[-limit:]:
            raw = self.requests.get(req_id)
            if not raw:
                continue
            e = raw if include_secrets else _redact(raw)

            def _hdr(d):
                return [{"name": k, "value": str(v)} for k, v in (d or {}).items()]

            entries.append({
                "startedDateTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(e.get("timestamp") or time.time())),
                "time": e.get("duration_ms") or 0,
                "request": {
                    "method": e.get("method") or "GET", "url": e.get("url") or "",
                    "headers": _hdr(e.get("headers")),
                    "postData": {"text": e.get("request_body") or ""} if e.get("request_body") else None,
                },
                "response": {
                    "status": e.get("status") if isinstance(e.get("status"), int) else 0,
                    "statusText": e.get("status_text") or "",
                    "headers": _hdr(e.get("response_headers")),
                    "content": {"mimeType": e.get("mime_type") or "", "text": e.get("response_body") or ""},
                },
            })
        return {"log": {"version": "1.2", "creator": {"name": "AgentBrowser", "version": "0.1.0"}, "entries": entries}}

    _BODY_SECRET_TOKENS = (
        "password", "passwd", "token", "secret", "authorization", "auth",
        "jwt", "session", "api_key", "apikey", "credential", "ssn", "card",
    )

    @classmethod
    def _redact_body_preview(cls, body: str, limit: int) -> str:
        """Preview a body with secret-looking fields masked (JSON + form)."""
        import re as _re

        text = (body or "").strip()

        def _mask(obj):
            if isinstance(obj, dict):
                return {
                    k: ("[redacted]" if any(tok in str(k).lower() for tok in cls._BODY_SECRET_TOKENS) else _mask(v))
                    for k, v in obj.items()
                }
            if isinstance(obj, list):
                return [_mask(v) for v in obj]
            return obj

        try:
            parsed = json.loads(text)
            text = json.dumps(_mask(parsed), ensure_ascii=False)
        except Exception:
            text = _re.sub(
                r"((?:password|passwd|token|secret|authorization|jwt|session|api[-_]?key)\s*[=:]\s*)([^\s&;\"']+)",
                r"\1[redacted]", text, flags=_re.I)
            text = _re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]+", r"\1[redacted]", text)
        return text[:limit] + ("..." if len(text) > limit else "")

    def format_ai_summary(self, limit: int = 25, exclude_assets: bool = True) -> str:
        """
        Token-Efficient summary table tailored specifically for LLM context windows.
        Strips noise headers and static assets, highlighting API payloads and auth tokens.
        """
        lines = ["=== CAPTURED NETWORK TRAFFIC (AI OPTIMIZED) ==="]
        count = 0
        ignored_extensions = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff", ".woff2", ".ttf", ".css", ".ico")

        for req_id in reversed(self.traffic_order):
            entry = self.requests.get(req_id)
            if not entry:
                continue

            url = entry.get("url", "")
            if exclude_assets and any(url.lower().split("?")[0].endswith(ext) for ext in ignored_extensions):
                continue

            method = entry.get("method", "GET")
            status = entry.get("status") or "PENDING"
            m_type = entry.get("mime_type") or entry.get("resource_type") or "Unknown"
            duration = f"{entry.get('duration_ms')}ms" if entry.get("duration_ms") else "..."

            lines.append(f"\n[#{entry.get('id')}] {method} {url}")
            lines.append(f"  Status: {status} | Type: {m_type} | Duration: {duration}")

            # Default AI summary redacts secret headers. Stored entries keep them
            # so an explicit traffic search can still match a cookie.
            headers = entry.get("headers") or {}
            redacted = redact_mapping(headers)
            secret_names = []
            for key, value in headers.items():
                if str(key).lower() in ("cookie", "authorization", "proxy-authorization", "x-api-key") and value:
                    secret_names.append(key)
            cookie_header = entry.get("request_cookie_header")
            if cookie_header and "cookie" not in {n.lower() for n in secret_names}:
                secret_names.append("Cookie")
            if secret_names:
                lines.append(f"  Secrets: {', '.join(secret_names)}=[redacted]")
            elif any(v == "[redacted]" for v in redacted.values()):
                lines.append("  Secrets: [redacted]")

            # Request body preview (secret fields redacted: this output is
            # built for LLM context, so passwords/tokens/PII must not print).
            req_body = entry.get("request_body")
            if req_body:
                preview = self._redact_body_preview(str(req_body), 200)
                lines.append(f"  Request Payload: {preview}")

            # Response body preview (same redaction; token responses count).
            res_body = entry.get("response_body")
            if res_body:
                preview = self._redact_body_preview(str(res_body), 300)
                lines.append(f"  Response Body: {preview}")

            # Initiator summary
            init = entry.get("initiator", {})
            if init.get("type") == "script":
                stack = init.get("stack", {}).get("callFrames", [])
                page_frames = [
                    f for f in stack
                    if "__agent_internal_" not in (f.get("url") or "")
                    and not (f.get("url") or "").startswith("__agent_")
                ]
                top = (page_frames or stack or [None])[0]
                if top:
                    lines.append(
                        f"  Caller: {top.get('url')}:{top.get('lineNumber')}:{top.get('columnNumber')} "
                        f"in {top.get('functionName') or 'anonymous'}"
                    )

            count += 1
            if count >= limit:
                break

        if count == 0:
            lines.append("  (No relevant API or document requests captured yet)")

        return "\n".join(lines)

