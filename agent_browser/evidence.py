"""
Evidence model for browser observations.

This module does not claim causation from a time window. It labels every
observation with the evidence that actually exists, and it only emits a
causal link when a concrete signal supports one.
"""

import time
from typing import Any, Dict, List, Optional


CONFIDENCE_TEMPORAL = "temporal"
CONFIDENCE_SUPPORTED = "supported"
CONFIDENCE_WEAK = "weak"
# A request/exception whose async initiator chain contains the agent action's
# stamped sourceURL was provably dispatched by that action (happens-before).
CONFIDENCE_ATTRIBUTED = "attributed"
CONFIDENCE_PROVEN = CONFIDENCE_ATTRIBUTED

ACTION_TAG_PREFIX = "__agent_internal_action_"


def make_action_tag(action_id: Any) -> str:
    """SourceURL marker stamped on the script that dispatches an agent action."""
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in str(action_id))
    return f"{ACTION_TAG_PREFIX}{safe}"


def _iter_stack_frames(stack: Any, _depth: int = 0):
    """Yield frames across a CDP stack dict (following ``parent``) or a frame list."""
    if _depth > 64 or stack is None:
        return
    if isinstance(stack, list):
        for frame in stack:
            if isinstance(frame, dict):
                yield frame
        return
    if not isinstance(stack, dict):
        return
    for frame in stack.get("callFrames") or []:
        if isinstance(frame, dict):
            yield frame
    parent = stack.get("parent")
    if parent is not None:
        yield from _iter_stack_frames(parent, _depth + 1)


def stack_contains_marker(stack: Any, marker: Optional[str]) -> bool:
    """True when any frame URL or stack description in the chain contains marker."""
    if not marker:
        return False
    if isinstance(stack, dict) and marker in str(stack.get("description") or ""):
        return True
    return any(marker in str(f.get("url") or "") for f in _iter_stack_frames(stack))


def initiator_chain(initiator: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten an initiator's full async chain (sync frames + parent links)."""
    if not initiator or not isinstance(initiator, dict):
        return []
    out = []
    for f in _iter_stack_frames(initiator.get("stack")):
        out.append({
            "url": f.get("url"),
            "line": f.get("lineNumber"),
            "column": f.get("columnNumber"),
            "function": f.get("functionName") or "anonymous",
        })
    return out


def request_attributed_to(entry: Dict[str, Any], action_tag: Optional[str]) -> bool:
    if not action_tag:
        return False
    initiator = entry.get("initiator") or {}
    return stack_contains_marker(initiator.get("stack"), action_tag)


def exception_attributed_to(exception: Dict[str, Any], action_tag: Optional[str]) -> bool:
    if not action_tag:
        return False
    return stack_contains_marker(exception.get("stack"), action_tag) or action_tag in str(
        exception.get("url") or ""
    )

# Substrings that indicate a script may have consumed a failed response.
# Used only as a weak signal, never as proof.
_NULL_CONSUMER_MARKERS = (
    "cannot read properties of undefined",
    "cannot read properties of null",
    "null is not an object",
    "undefined is not an object",
    "fetch failed",
)


def initiator_top(initiator: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Return the top *page* script frame of a CDP initiator, or None.

    Agent-internal hook frames (``__agent_internal_*`` / ``__agent_*``) sit on
    top of the stack whenever the fetch/XHR hooks are installed; reporting
    them as the caller would blame the instrumentation, not the page.
    """
    if not initiator or not isinstance(initiator, dict):
        return None
    if initiator.get("type") != "script":
        return None
    frames = (initiator.get("stack") or {}).get("callFrames") or []
    if not frames:
        return None
    page_frames = [
        f for f in frames
        if "__agent_internal_" not in (f.get("url") or "")
        and not (f.get("url") or "").startswith("__agent_")
    ]
    top = (page_frames or frames)[0]
    return {
        "url": top.get("url"),
        "line": top.get("lineNumber"),
        "column": top.get("columnNumber"),
        "function": top.get("functionName") or "anonymous",
    }


def _url_host_path(url: Optional[str]) -> str:
    if not url:
        return ""
    cleaned = url.split("#", 1)[0]
    # Drop scheme for a cheap overlap check. Not a parser.
    if "://" in cleaned:
        cleaned = cleaned.split("://", 1)[1]
    return cleaned.lower()


def _exception_mentions_url(exception: Dict[str, Any], url: Optional[str]) -> bool:
    if not url:
        return False
    path = _url_host_path(url)
    if not path:
        return False
    blob = " ".join(
        str(exception.get(k) or "")
        for k in ("description", "text", "url")
    ).lower()
    # Require a meaningful suffix so a bare host does not match everything.
    needle = path[-80:]
    return needle in blob and len(needle) >= 8


def _listener_reached(
    action_type: str,
    mutations: List[Dict[str, Any]],
    observed_exceptions: List[Dict[str, Any]],
) -> bool:
    """
    A click reached the element listener only when the page itself changed.

    Dispatching the input is not enough. The listener on the fixture writes
    text and then throws, so both signals have to be present.
    """
    if action_type != "click":
        return False
    return bool(mutations) and bool(observed_exceptions)


def _is_failing(entry: Dict[str, Any]) -> bool:
    status = entry.get("status")
    if status == "FAILED":
        return True
    return isinstance(status, int) and status >= 400


def _is_actionable_failure(entry: Dict[str, Any]) -> bool:
    """
    A favicon or image 404 is a failure, but it must not cancel the
    single-failure signal for an API request.
    """
    if not _is_failing(entry):
        return False
    rtype = (entry.get("resource_type") or "").lower()
    if rtype in ("image", "font", "media", "stylesheet"):
        return False
    url = (entry.get("url") or "").lower().split("?", 1)[0].split("#", 1)[0]
    if url.endswith("/favicon.ico") or url.endswith(".ico"):
        return False
    # A refused navigation is a page-load error, not an API the click consumed.
    error_text = (entry.get("error_text") or "").lower()
    if "err_unsafe_port" in error_text or "err_connection_refused" in error_text:
        return False
    if rtype == "document" and entry.get("status") == "FAILED":
        return False
    return True


def build_attribution_links(
    requests: List[Dict[str, Any]],
    action_tag: Optional[str],
) -> List[Dict[str, Any]]:
    """One attributed link per request whose initiator chain contains the action tag."""
    if not action_tag:
        return []
    links: List[Dict[str, Any]] = []
    for req in requests:
        attributed = req.get("confidence") == CONFIDENCE_ATTRIBUTED or request_attributed_to(req, action_tag)
        if not attributed:
            continue
        links.append({
            "kind": "stack_attribution",
            "confidence": CONFIDENCE_ATTRIBUTED,
            "evidence": {
                "request_id": req.get("id"),
                "url": req.get("url"),
                "status": req.get("status"),
                "action_tag": action_tag,
                "rationale": "Initiator async stack chain contains the agent action's stamped sourceURL.",
            },
        })
    return links


def build_causal_links(
    exceptions: List[Dict[str, Any]],
    requests: List[Dict[str, Any]],
    action_tag: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Emit a link only when evidence exists.

    Attributed: request initiator chain contains the action tag (proof).
    Supported: exception text or URL contains the failing request URL.
    Weak: exception looks like a null-consumer AND exactly one failing
    request was observed. Multiple failing requests make that signal
    ambiguous, so no link is emitted.
    """
    links: List[Dict[str, Any]] = list(build_attribution_links(requests, action_tag))
    failing = [r for r in requests if _is_actionable_failure(r)]
    seen = {(l["kind"], l["evidence"].get("request_id")) for l in links}

    for ex in exceptions:
        for req in failing:
            url = req.get("url")
            if _exception_mentions_url(ex, url):
                key = ("url_overlap", req.get("id"), ex.get("description"))
                if key in seen:
                    continue
                seen.add(key)
                links.append({
                    "kind": "url_overlap",
                    "confidence": CONFIDENCE_SUPPORTED,
                    "evidence": {
                        "request_id": req.get("id"),
                        "url": url,
                        "status": req.get("status"),
                        "exception": (ex.get("description") or ex.get("text") or "")[:180],
                    },
                })

    if len(failing) == 1:
        req = failing[0]
        for ex in exceptions:
            desc = (ex.get("description") or ex.get("text") or "").lower()
            if any(marker in desc for marker in _NULL_CONSUMER_MARKERS):
                key = ("single_failure_null_consumer", req.get("id"), desc[:80])
                if key in seen:
                    continue
                seen.add(key)
                links.append({
                    "kind": "single_failure_null_consumer",
                    "confidence": CONFIDENCE_WEAK,
                    "evidence": {
                        "request_id": req.get("id"),
                        "url": req.get("url"),
                        "status": req.get("status"),
                        "exception": (ex.get("description") or ex.get("text") or "")[:180],
                        "rationale": "Only one failing request was observed in the window, and the exception looks like a null consumer. This is not proof.",
                    },
                })
    return links


def summarize_request(
    entry: Dict[str, Any],
    in_window_reason: str,
    action_tag: Optional[str] = None,
) -> Dict[str, Any]:
    initiator = entry.get("initiator") or {}
    attributed = request_attributed_to(entry, action_tag)
    return {
        "id": entry.get("id"),
        "url": entry.get("url"),
        "method": entry.get("method"),
        "status": entry.get("status"),
        "resource_type": entry.get("resource_type"),
        "duration_ms": entry.get("duration_ms"),
        "error_text": entry.get("error_text"),
        "initiator_type": initiator.get("type"),
        "initiator_top": initiator_top(initiator),
        "initiator_chain": initiator_chain(initiator)[:12],
        "confidence": CONFIDENCE_ATTRIBUTED if attributed else CONFIDENCE_TEMPORAL,
        "caused_by_action": attributed,
        "in_window_reason": in_window_reason,
        "same_request_id_as_action": attributed,
    }


def slice_since(items: List[Any], start_index: int) -> List[Any]:
    """
    Slice a list that may have been evicted from the front.

    If start_index is past the current length, the original window was
    entirely evicted and the result is empty rather than a shifted tail.
    """
    if start_index < 0:
        start_index = 0
    if start_index > len(items):
        return []
    return list(items[start_index:])


def build_trace_report(
    *,
    action_type: str,
    target: Optional[str],
    params: Dict[str, Any],
    action_error: Optional[str],
    action_result: Any,
    start_time: float,
    duration_ms: float,
    pre_url: str,
    post_url: str,
    post_title: str,
    mutations: List[Dict[str, Any]],
    mutation_truncated: bool,
    observed_requests: List[Dict[str, Any]],
    observed_exceptions: List[Dict[str, Any]],
    observed_console_errors: List[Dict[str, Any]],
    window_note: str,
    action_tag: Optional[str] = None,
    settle_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble an honest trace. Temporal observations are never called causes."""
    failing = [r for r in observed_requests if _is_failing(r)]
    links = build_causal_links(observed_exceptions, observed_requests, action_tag=action_tag)

    observations: List[str] = []
    if action_error:
        observations.append(f"Action failed during execution: {action_error}")
    if observed_requests:
        observations.append(
            f"Observed {len(observed_requests)} request(s) appended during the settle window. "
            "This is temporal co-occurrence, not a proven trigger."
        )
    for req in failing:
        observations.append(
            f"Observed HTTP failure during window: {req.get('method')} {req.get('url')} "
            f"-> {req.get('status')} ({req.get('error_text') or ''}). confidence={req.get('confidence') or CONFIDENCE_TEMPORAL}"
        )
    for ex in observed_exceptions:
        observations.append(
            f"Observed uncaught exception during window: {ex.get('description') or ex.get('text')} "
            f"at {ex.get('url')}:{ex.get('line')}. confidence=temporal"
        )
    for link in links:
        ev = link["evidence"]
        detail = ev.get("exception") or ev.get("rationale") or ev.get("url") or ""
        observations.append(
            f"Causal link ({link['confidence']}): {link['kind']} "
            f"request={ev.get('request_id')} {detail}".rstrip()
        )
    if mutation_truncated:
        observations.append("DOM mutation buffer truncated at the in-page cap. Sample is incomplete.")
    listener_reached = _listener_reached(action_type, mutations, observed_exceptions)
    if listener_reached:
        observations.append(
            "Listener reached: the page recorded a DOM mutation and an uncaught exception "
            "after the click. The element action ran."
        )
    if (
        not action_error
        and not failing
        and not observed_exceptions
        and not observed_console_errors
    ):
        observations.append("No action error, failing request, or uncaught exception observed in the window.")

    return {
        "action": {
            "type": action_type,
            "target": target,
            "params": params,
            "success": action_error is None,
            "error": action_error,
            "result": action_result,
            "listener_reached": listener_reached,
            "page_idle": not listener_reached and not mutations and not observed_exceptions,
        },
        "timeline": {
            "duration_ms": duration_ms,
            "timestamp": start_time,
            "t_end": start_time + (duration_ms / 1000.0),
        },
        "evidence": {
            "method": "stack_attribution" if action_tag else "index_window",
            "note": window_note,
            "action_tag": action_tag,
            "causal_links": links,
            "link_count": len(links),
            "settle": settle_info,
        },
        "navigation": {
            "navigated": pre_url != post_url,
            "from_url": pre_url,
            "to_url": post_url,
            "title": post_title,
        },
        "dom_changes": {
            "mutation_count": len(mutations),
            "truncated": mutation_truncated,
            "sample_mutations": mutations[:15],
        },
        "network": {
            "requests_observed_count": len(observed_requests),
            # A settle-window request is not a trigger. A weak single-failure
            # link is not a trigger either. Only a supported (URL overlap) or
            # attributed (stack contains the action tag) link names a request
            # this action triggered.
            "requests_triggered_count": len({
                link.get("evidence", {}).get("request_id")
                for link in links
                if link.get("confidence") in (CONFIDENCE_SUPPORTED, CONFIDENCE_ATTRIBUTED)
                and link.get("evidence", {}).get("request_id")
            }),
            "requests": observed_requests,
            "failing_requests_count": len(failing),
        },
        "runtime": {
            "exceptions_count": len(observed_exceptions),
            "exceptions": observed_exceptions,
            "console_errors_count": len(observed_console_errors),
        },
        "diagnostics": observations,
    }


def correlate_diagnostics(
    exceptions: List[Dict[str, Any]],
    requests: List[Dict[str, Any]],
    action_tag: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Ranked hypotheses for the diagnostic engine.

    Never pairs every exception with every failing request.
    """
    links = build_causal_links(exceptions, requests, action_tag=action_tag)
    hypotheses = []
    for link in links:
        hypotheses.append({
            "status": "supported" if link["confidence"] in (CONFIDENCE_SUPPORTED, CONFIDENCE_ATTRIBUTED) else "weak",
            "confidence": link["confidence"],
            "kind": link["kind"],
            "message": (
                f"{link['kind']}: {link['evidence'].get('exception')} "
                f"~ {link['evidence'].get('url')} ({link['evidence'].get('status')})"
            ),
            "evidence": link["evidence"],
        })
    if not hypotheses and any(_is_failing(r) for r in requests) and exceptions:
        hypotheses.append({
            "status": "insufficient_evidence",
            "confidence": None,
            "kind": "none",
            "message": (
                "Exceptions and failing requests were both observed, but no URL overlap "
                "and no single-failure signal. Not attributed."
            ),
            "evidence": {},
        })
    return hypotheses


def redact_mapping(data: Dict[str, Any], secret_keys: Optional[set] = None) -> Dict[str, Any]:
    """Return a copy with secret header values replaced. Does not mutate input."""
    secret_keys = secret_keys or {
        "authorization",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "x-api-key",
    }
    out = {}
    for key, value in data.items():
        if str(key).lower() in secret_keys:
            out[key] = "[redacted]" if value else value
        else:
            out[key] = value
    return out


_SECRET_HEADER_NAMES = {
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-api-key",
}


def redact_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a shallow copy of a stored network/proxy entry with secret-bearing
    header and cookie fields masked. Bodies are left intact for debugging.

    Redaction is applied on egress (API responses, exports) rather than at
    ingest, so the in-process buffer can still match a traffic search by cookie,
    but secrets never leave the daemon unless the caller explicitly opts in.
    """
    if not isinstance(entry, dict):
        return entry
    out = dict(entry)
    if isinstance(out.get("headers"), dict):
        out["headers"] = redact_mapping(out["headers"])
    if isinstance(out.get("response_headers"), dict):
        out["response_headers"] = redact_mapping(out["response_headers"])
    raw = out.get("response_headers_raw")
    if isinstance(raw, list):
        out["response_headers_raw"] = [
            ({**h, "value": "[redacted]"} if str(h.get("name", "")).lower() in _SECRET_HEADER_NAMES else h)
            for h in raw
            if isinstance(h, dict)
        ]
    if out.get("request_cookie_header"):
        out["request_cookie_header"] = "[redacted]"
    for cookie_field in ("request_cookies", "response_cookies"):
        if out.get(cookie_field):
            out[cookie_field] = "[redacted]"
    return out


def cookie_url_for_domain(domain: str, path: str) -> str:
    """
    Build a Network.setCookie url for a host-only cookie.

    A host that already contains a port must not have the path appended
    after a colon.
    """
    path = path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return f"http://{domain}{path}"


def is_within_dir(path: str, root: str) -> bool:
    """True if path is root or a descendant. Separator-safe."""
    import os
    path = os.path.abspath(path)
    root = os.path.abspath(root)
    if path == root:
        return True
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        # Different drives on Windows.
        return False


_VOLATILE_QUERY_KEYS = frozenset({
    "_", "t", "ts", "cb", "cachebust", "nocache", "nonce", "rand", "random", "timestamp", "_dc", "v",
})


def url_template(url: Optional[str], *, keep_query_values: bool = False) -> str:
    """
    Low-cardinality URL template for failure signatures: host + path with
    numeric/uuid/hex segments collapsed to ``{n}``/``{uuid}``/``{hex}`` and
    volatile query keys dropped (values blanked unless requested). Two URLs
    that differ only in ids/tokens map to the same template.
    """
    import re as _re
    from urllib.parse import parse_qsl, urlsplit

    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except Exception:
        return str(url)
    host = (parts.hostname or "").lower()
    if parts.port and not (parts.scheme == "http" and parts.port == 80) and not (parts.scheme == "https" and parts.port == 443):
        host = f"{host}:{parts.port}"
    segs = []
    for seg in parts.path.split("/"):
        if _re.fullmatch(r"\d+", seg or ""):
            segs.append("{n}")
        elif _re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", seg or "", _re.I):
            segs.append("{uuid}")
        elif _re.fullmatch(r"[0-9a-f]{12,}", seg or "", _re.I):
            segs.append("{hex}")
        else:
            segs.append(seg)
    pairs = [(k.strip(), v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.strip().lower() not in _VOLATILE_QUERY_KEYS]
    query = "&".join(f"{k}={v if keep_query_values else ''}" for k, v in sorted(pairs))
    path = "/".join(segs) or "/"
    return f"{host}{path}{'?' + query if query else ''}"


_V8_MESSAGE_TEMPLATES = (
    ("Cannot read properties of % (reading '%')", "V8.NonObjectPropertyLoad"),
    ("Cannot set properties of % (setting '%')", "V8.NonObjectPropertyStore"),
    ("Cannot read properties of %", "V8.NonObjectPropertyLoad"),
    ("% is not a function", "V8.NotCallable"),
    ("% is not defined", "V8.NotDefined"),
    ("% is not iterable", "V8.NotIterable"),
    ("% is not a constructor", "V8.NotConstructor"),
    ("Maximum call stack size exceeded", "V8.StackOverflow"),
    ("Unexpected end of JSON input", "V8.JsonParseUnexpectedEOS"),
)


def normalize_exception_message(message: Optional[str]) -> Dict[str, str]:
    """
    Normalize an exception message into {template_id, template}: volatile
    numbers/hex/URLs collapse so the same bug at a different id groups into
    one signature, while the meaningful slots (property names) survive.
    """
    import re as _re

    first = (message or "").split("\n", 1)[0].strip()
    # Strip a leading "TypeError: " / "Uncaught (in promise) TypeError: " so the
    # V8 message templates (which describe the message body) can match.
    import re as _re2

    first = _re2.sub(r"^(?:Uncaught(?: \(in promise\))? )?[A-Z][\w$]*(?:Error|Exception):\s*", "", first)
    for template, tid in _V8_MESSAGE_TEMPLATES:
        pattern = "^" + "(.+?)".join(_re.escape(p) for p in template.split("%")) + "$"
        m = _re.match(pattern, first)
        if m:
            out = template
            for group in m.groups():
                out = out.replace("%", group, 1)
            return {"template_id": tid, "template": out[:200]}
    out = _re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<uuid>", first, flags=_re.I)
    out = _re.sub(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", "<hex>", out, flags=_re.I)
    out = _re.sub(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])", "<num>", out)
    return {"template_id": "generic", "template": out[:200]}


def now() -> float:
    return time.time()
