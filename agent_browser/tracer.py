import time
import asyncio
import json
from typing import Dict, Any, List, Optional, Callable


INJECT_MUTATION_TRACKER_JS = r"""
(() => {
    if (window.__agentMutationTracker) {
        window.__agentMutationTracker.mutations = [];
        return;
    }

    const tracker = {
        mutations: [],
        observer: null
    };

    tracker.observer = new MutationObserver((mutationsList) => {
        for (let m of mutationsList) {
            if (tracker.mutations.length > 200) break;
            if (m.type === 'childList') {
                for (let n of m.addedNodes) {
                    if (n.nodeType === Node.ELEMENT_NODE) {
                        tracker.mutations.push({
                            type: 'added',
                            tag: n.tagName.toLowerCase(),
                            id: n.id || undefined,
                            text: (n.innerText || n.textContent || '').trim().slice(0, 80)
                        });
                    }
                }
                for (let n of m.removedNodes) {
                    if (n.nodeType === Node.ELEMENT_NODE) {
                        tracker.mutations.push({
                            type: 'removed',
                            tag: n.tagName.toLowerCase(),
                            id: n.id || undefined,
                            text: (n.innerText || n.textContent || '').trim().slice(0, 80)
                        });
                    }
                }
            } else if (m.type === 'attributes') {
                tracker.mutations.push({
                    type: 'attribute_changed',
                    tag: m.target.tagName ? m.target.tagName.toLowerCase() : '',
                    attribute: m.attributeName,
                    newValue: m.target.getAttribute(m.attributeName)
                });
            }
        }
    });

    tracker.observer.observe(document.body || document.documentElement, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['class', 'style', 'disabled', 'hidden', 'aria-expanded', 'aria-selected', 'value']
    });

    window.__agentMutationTracker = tracker;
})()
"""

GET_MUTATIONS_JS = r"""
(() => {
    if (!window.__agentMutationTracker) return [];
    const list = window.__agentMutationTracker.mutations;
    window.__agentMutationTracker.mutations = [];
    return list;
})()
"""

class ActionTracer:
    """
    Action & Correlation Tracer.
    Traces user interactions (clicks, inputs, navigations) and tracks causal downstream
    effects across DOM mutations, HTTP requests, console logs, and runtime errors.
    """
    def __init__(self, cdp_conn, dom_engine, network_engine, runtime_engine, active_session_id_getter):
        self.cdp = cdp_conn
        self.dom = dom_engine
        self.network = network_engine
        self.runtime = runtime_engine
        self.get_session_id = active_session_id_getter

    async def trace_action(
        self,
        action_type: str,
        target: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        settle_timeout: float = 1.0
    ) -> Dict[str, Any]:
        """
        Executes an action while tracing all causal downstream events.
        action_type: 'click', 'type', 'navigate', 'eval', 'hover', 'scroll'
        """
        params = params or {}
        sid = self.get_session_id()

        # 1. Capture Pre-action state
        pre_url = ""
        pre_title = ""
        try:
            res = await self.cdp.send("Runtime.evaluate", {
                "expression": "({ url: window.location.href, title: document.title })",
                "returnByValue": True
            }, session_id=sid, timeout=3.0)
            val = res.get("result", {}).get("value", {})
            pre_url = val.get("url", "")
            pre_title = val.get("title", "")
        except Exception:
            pass

        # Reset in-page mutation observer
        try:
            await self.cdp.send("Runtime.evaluate", {"expression": INJECT_MUTATION_TRACKER_JS}, session_id=sid, timeout=2.0)
        except Exception:
            pass

        start_time = time.time()
        start_req_count = len(self.network.traffic_order)
        start_err_count = len(self.runtime.exceptions)
        start_log_count = len(self.runtime.console_logs)

        action_error = None
        action_result = None

        # 2. Execute Action
        try:
            if action_type == "click":
                action_result = await self.dom.click(target)
            elif action_type == "type":
                text = params.get("text", "")
                clear_first = params.get("clear_first", True)
                action_result = await self.dom.type_text(target, text, clear_first=clear_first)
            elif action_type == "navigate":
                url = target or params.get("url", "")
                if not url.startswith("http://") and not url.startswith("https://") and not url.startswith("about:"):
                    url = "https://" + url
                action_result = await self.cdp.send("Page.navigate", {"url": url}, session_id=sid)
            elif action_type == "eval":
                expr = target or params.get("expression", "")
                action_result = await self.runtime.evaluate(expr)
            elif action_type == "hover":
                el = await self.dom.query_selector(target)
                if not el or not el.get("rect"):
                    raise ValueError(f"Element '{target}' not found.")
                rect = el["rect"]
                cx = rect["x"] + rect["width"] / 2
                cy = rect["y"] + rect["height"] / 2
                await self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": cx, "y": cy}, session_id=sid)
                action_result = {"hovered": True, "coordinates": {"x": cx, "y": cy}}
            elif action_type == "scroll":
                delta_y = params.get("delta_y", 400)
                await self.cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseWheel",
                    "x": 300,
                    "y": 300,
                    "deltaX": 0,
                    "deltaY": delta_y
                }, session_id=sid)
                action_result = {"scrolled": True, "delta_y": delta_y}
            else:
                raise ValueError(f"Unsupported action type: {action_type}")
        except Exception as e:
            action_error = str(e)

        # 3. Settle wait to collect asynchronous downstream effects
        await asyncio.sleep(settle_timeout)

        duration_ms = round((time.time() - start_time) * 1000, 2)

        # 4. Capture Post-action state
        post_url = pre_url
        post_title = pre_title
        try:
            res = await self.cdp.send("Runtime.evaluate", {
                "expression": "({ url: window.location.href, title: document.title })",
                "returnByValue": True
            }, session_id=sid, timeout=3.0)
            val = res.get("result", {}).get("value", {})
            post_url = val.get("url", "")
            post_title = val.get("title", "")
        except Exception:
            pass

        # Harvest triggered mutations
        mutations = []
        try:
            m_res = await self.cdp.send("Runtime.evaluate", {"expression": GET_MUTATIONS_JS, "returnByValue": True}, session_id=sid, timeout=2.0)
            mutations = m_res.get("result", {}).get("value", []) or []
        except Exception:
            pass

        # Harvest triggered network requests
        triggered_request_ids = self.network.traffic_order[start_req_count:]
        triggered_requests = []
        failing_requests = []
        for rid in triggered_request_ids:
            r = self.network.get_request(rid)
            if r:
                summary = {
                    "id": rid,
                    "url": r.get("url"),
                    "method": r.get("method"),
                    "status": r.get("status"),
                    "resource_type": r.get("resource_type"),
                    "duration_ms": r.get("duration_ms")
                }
                triggered_requests.append(summary)
                if r.get("status") in ("FAILED",) or (isinstance(r.get("status"), int) and r.get("status") >= 400):
                    failing_requests.append(r)

        # Harvest triggered runtime exceptions and console errors
        triggered_exceptions = self.runtime.exceptions[start_err_count:]
        triggered_logs = self.runtime.console_logs[start_log_count:]
        triggered_console_errors = [l for l in triggered_logs if l["type"] == "error"]

        # 5. Formulate Diagnostic Correlation
        diagnostics = []
        if action_error:
            diagnostics.append(f"Action failed directly during execution: {action_error}")
        if failing_requests:
            for fr in failing_requests:
                diagnostics.append(f"Action triggered HTTP failure: {fr.get('method')} {fr.get('url')} -> Status {fr.get('status')} ({fr.get('error_text') or ''})")
        if triggered_exceptions:
            for ex in triggered_exceptions:
                diagnostics.append(f"Action triggered Uncaught JS Exception: {ex.get('description') or ex.get('text')} at {ex.get('url')}:{ex.get('line')}")
        if not action_error and not failing_requests and not triggered_exceptions:
            diagnostics.append("Action executed cleanly with zero network or runtime errors.")

        report = {
            "action": {
                "type": action_type,
                "target": target,
                "params": params,
                "success": action_error is None,
                "error": action_error,
                "result": action_result
            },
            "timeline": {
                "duration_ms": duration_ms,
                "timestamp": start_time
            },
            "navigation": {
                "navigated": (pre_url != post_url),
                "from_url": pre_url,
                "to_url": post_url,
                "title": post_title
            },
            "dom_changes": {
                "mutation_count": len(mutations),
                "sample_mutations": mutations[:15]
            },
            "network": {
                "requests_triggered_count": len(triggered_requests),
                "requests": triggered_requests,
                "failing_requests_count": len(failing_requests)
            },
            "runtime": {
                "exceptions_count": len(triggered_exceptions),
                "exceptions": triggered_exceptions,
                "console_errors_count": len(triggered_console_errors)
            },
            "diagnostics": diagnostics
        }

        return report
