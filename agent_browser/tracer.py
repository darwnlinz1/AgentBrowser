import time
import asyncio
from typing import Dict, Any, List, Optional, Callable

from agent_browser.evidence import build_trace_report, make_action_tag, slice_since, summarize_request
from agent_browser.quiescence import QuiescenceTracker, wait_for_quiescence


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

    tracker.truncated = false;
    tracker.observer = new MutationObserver((mutationsList) => {
        for (let m of mutationsList) {
            if (tracker.mutations.length >= 200) {
                tracker.truncated = true;
                break;
            }
            if (m.type === 'childList') {
                for (let n of m.addedNodes) {
                    if (tracker.mutations.length >= 200) {
                        tracker.truncated = true;
                        break;
                    }
                    if (n.nodeType === Node.ELEMENT_NODE) {
                        tracker.mutations.push({
                            type: 'added',
                            tag: n.tagName.toLowerCase(),
                            id: n.id || undefined,
                            text: (n.innerText || n.textContent || '').trim().slice(0, 80)
                        });
                    } else if (n.nodeType === Node.TEXT_NODE && (n.textContent || '').trim()) {
                        tracker.mutations.push({
                            type: 'text',
                            text: (n.textContent || '').trim().slice(0, 80)
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
            } else if (m.type === 'characterData') {
                tracker.mutations.push({
                    type: 'text',
                    text: (m.target.textContent || '').trim().slice(0, 80)
                });
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
        characterData: true,
        attributes: true,
        attributeFilter: ['class', 'style', 'disabled', 'hidden', 'aria-expanded', 'aria-selected', 'value']
    });

    window.__agentMutationTracker = tracker;
})()
"""

GET_MUTATIONS_JS = r"""
(() => {
    if (!window.__agentMutationTracker) return { mutations: [], truncated: false };
    const tracker = window.__agentMutationTracker;
    const list = tracker.mutations;
    const truncated = !!tracker.truncated;
    tracker.mutations = [];
    tracker.truncated = false;
    return { mutations: list, truncated: truncated };
})()
"""

class ActionTracer:
    """
    Action & Correlation Tracer.
    Traces user interactions (clicks, inputs, navigations) and tracks causal downstream
    effects across DOM mutations, HTTP requests, console logs, and runtime errors.
    """
    # Strict settle probe: requestIdleCallback only fires when no runnable
    # task exists; its deadline reveals whether a timer is due imminently.
    IDLE_PROBE_JS = (
        "(async () => {"
        " const dl = await new Promise(r => typeof requestIdleCallback === 'function'"
        " ? requestIdleCallback(d => r({idle: !d.didTimeout, rem: d.timeRemaining()}), {timeout: 200})"
        " : r({idle: true, rem: -1}));"
        " return {idle: dl.idle, rem: dl.rem,"
        " busy: document.querySelectorAll('[aria-busy=\"true\"]').length};"
        "})()"
    )

    def __init__(self, cdp_conn, dom_engine, network_engine, runtime_engine, active_session_id_getter, activity_monitor=None, strict_probe: bool = False):
        self.cdp = cdp_conn
        self.dom = dom_engine
        self.network = network_engine
        self.runtime = runtime_engine
        self.get_session_id = active_session_id_getter
        # Optional synchronous NetworkActivityMonitor (never-dropped ledger).
        self.activity_monitor = activity_monitor
        # Opt-in strict mode: also require a deep idle callback (no timer
        # due within the idle window, no aria-busy region) before settling.
        self.strict_probe = strict_probe

    async def _settle(self, start_req_count: int, timeout_s: float, action_type: str = "") -> Dict[str, Any]:
        """Feed a QuiescenceTracker from the network buffer until quiet/cap."""
        # Typing gets a floor: debounced handlers (100-300ms) have not fired
        # when the keystrokes land, so an empty ledger is not yet quiet.
        floor_s = 0.35 if action_type == "type" else 0.0
        tracker = QuiescenceTracker(quiet_window_s=0.25, floor_s=floor_s)
        seen_finished: set = set()

        monitor = self.activity_monitor

        async def _poll():
            if monitor is not None:
                # The sync ledger is authoritative for in-flight state: it
                # cannot have lost an event to the async handler cap.
                snap = monitor.snapshot()
                # SPA route window: a route change since the last poll
                # opens a fresh tracker epoch; previous-route stragglers
                # stop blocking this route's settle.
                while tracker.route_epoch < snap.get("route_epoch", 0):
                    tracker.note_route_change()
                current = snap.get("in_flight_current_route", snap["in_flight"])
                for rid in current:
                    tracker.note_request_start(rid)
                for rid in list(tracker.in_flight):
                    if rid not in current:
                        tracker.note_request_end(rid)
                if snap["last_activity"] > tracker.last_activity:
                    tracker.last_activity = snap["last_activity"]
                return
            ids = slice_since(self.network.traffic_order, start_req_count)
            for rid in ids:
                entry = self.network.get_request(rid)
                if not entry:
                    continue
                if entry.get("status") is None:
                    tracker.note_request_start(rid, entry.get("resource_type"), entry.get("url"))
                elif rid not in seen_finished:
                    seen_finished.add(rid)
                    tracker.note_request_end(rid)

        result = await wait_for_quiescence(tracker, timeout_s=max(0.2, timeout_s), poll=_poll)
        result["activity_source"] = "sync_monitor" if monitor is not None else "traffic_buffer"

        # Stamped-probe verification: a quiet reading can be stale -- activity
        # may have started while the tracker was concluding. Stamp the
        # monitor's activity sequence, run one main-thread probe (a plain
        # Runtime.evaluate round trip queues behind pending tasks/microtasks,
        # so its return proves the thread drained), and only trust the settle
        # if the sequence is unchanged across the probe. Otherwise re-wait.
        result["probe_verified"] = None
        if monitor is not None and result.get("settled"):
            import time as _time

            deadline = _time.monotonic() + max(0.2, timeout_s)
            for _ in range(3):
                seq_before = monitor.snapshot()["activity_seq"]
                try:
                    await self.runtime.evaluate("performance.now()")
                except Exception:
                    break
                seq_after = monitor.snapshot()["activity_seq"]
                if seq_after == seq_before:
                    if self.strict_probe:
                        idle_ok = await self._idle_probe()
                        result["idle_depth_ok"] = idle_ok
                        if not idle_ok:
                            result["probe_verified"] = False
                            if _time.monotonic() >= deadline:
                                break
                            result = await wait_for_quiescence(
                                tracker, timeout_s=max(0.2, deadline - _time.monotonic()), poll=_poll)
                            result["activity_source"] = "sync_monitor"
                            if not result.get("settled"):
                                break
                            continue
                    result["probe_verified"] = True
                    break
                result["probe_verified"] = False
                if _time.monotonic() >= deadline:
                    break
                result = await wait_for_quiescence(
                    tracker, timeout_s=max(0.2, deadline - _time.monotonic()), poll=_poll)
                result["activity_source"] = "sync_monitor"
                if not result.get("settled"):
                    break
        return result

    async def _idle_probe(self) -> bool:
        """Deep-idle check: idle callback fired, not shallow, no aria-busy."""
        try:
            res = await self.runtime.evaluate(self.IDLE_PROBE_JS)
        except Exception:
            return False
        value = res.get("value") if isinstance(res, dict) else None
        if not isinstance(value, dict):
            return False
        if not value.get("idle") or (value.get("busy") or 0):
            return False
        rem = value.get("rem", -1)
        return rem is None or rem < 0 or rem >= 8

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
        action_tag = make_action_tag(f"{int(start_time * 1000)}-{action_type}")

        action_error = None
        action_result = None

        # 2. Execute Action
        try:
            if action_type == "click":
                action_result = await self.dom.click(target, action_tag=action_tag)
            elif action_type == "type":
                text = params.get("text", "")
                clear_first = params.get("clear_first", True)
                action_result = await self.dom.type_text(target, text, clear_first=clear_first, action_tag=action_tag)
            elif action_type == "navigate":
                from agent_browser.cdp import wait_for_navigation
                url = target or params.get("url", "")
                action_result = await wait_for_navigation(self.cdp, sid, url)
                if not action_result.get("navigated", True):
                    action_error = action_result.get("error") or "navigation failed"
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

        # 3. Event-driven settle: wait until requests started after the action
        # have finished and activity is quiet, capped by settle_timeout as the
        # hard (deadlock-proof) bound. No fixed full-length sleep.
        settle_info = await self._settle(start_req_count, settle_timeout, action_type=action_type)

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

        # Harvest observed mutations. A request that merely arrived in the
        # settle window is an observation, not a proven trigger.
        mutations = []
        mutation_truncated = False
        try:
            m_res = await self.cdp.send("Runtime.evaluate", {"expression": GET_MUTATIONS_JS, "returnByValue": True}, session_id=sid, timeout=2.0)
            harvested = m_res.get("result", {}).get("value", []) or []
            if isinstance(harvested, dict):
                mutations = harvested.get("mutations") or []
                mutation_truncated = bool(harvested.get("truncated"))
            elif isinstance(harvested, list):
                mutations = harvested
        except Exception:
            pass

        observed_ids = slice_since(self.network.traffic_order, start_req_count)
        observed_requests = []
        for rid in observed_ids:
            entry = self.network.get_request(rid)
            if not entry:
                continue
            observed_requests.append(summarize_request(
                entry,
                "request appended after the pre-action index snapshot",
                action_tag=action_tag,
            ))

        observed_exceptions = slice_since(self.runtime.exceptions, start_err_count)
        observed_logs = slice_since(self.runtime.console_logs, start_log_count)
        observed_console_errors = [l for l in observed_logs if l.get("type") == "error"]

        window_note = (
            "Requests whose initiator async stack contains the action's stamped "
            f"sourceURL ({action_tag}) are labeled attributed (proven). Other "
            "requests in the window are temporal co-occurrence only and are not "
            "labeled as triggered by the action."
        )
        return build_trace_report(
            action_type=action_type,
            target=target,
            params=params,
            action_error=action_error,
            action_result=action_result,
            start_time=start_time,
            duration_ms=duration_ms,
            pre_url=pre_url,
            post_url=post_url,
            post_title=post_title,
            mutations=mutations,
            mutation_truncated=mutation_truncated,
            observed_requests=observed_requests,
            observed_exceptions=observed_exceptions,
            observed_console_errors=observed_console_errors,
            window_note=window_note,
            action_tag=action_tag,
            settle_info=settle_info,
        )
