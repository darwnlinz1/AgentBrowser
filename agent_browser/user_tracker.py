import time
import json
from typing import Dict, Any, List, Optional, Callable

USER_TRACKER_INJECT_JS = r"""
(() => {
    if (window.__agentUserTrackerInstalled) return;
    window.__agentUserTrackerInstalled = true;

    // Helper: calculate stable CSS selector
    function getCssSelector(el) {
        if (!(el instanceof Element)) return '';
        if (el.id) return '#' + el.id;
        
        let path = [];
        while (el && el.nodeType === Node.ELEMENT_NODE) {
            let selector = el.nodeName.toLowerCase();
            if (el.id) {
                selector += '#' + el.id;
                path.unshift(selector);
                break;
            } else {
                let sibling = el, nth = 1;
                while (sibling = sibling.previousElementSibling) {
                    if (sibling.nodeName.toLowerCase() === selector) nth++;
                }
                if (nth > 1) selector += `:nth-of-type(${nth})`;
            }
            path.unshift(selector);
            el = el.parentElement;
        }
        return path.join(' > ');
    }

    // Capture click / pointer events
    ['click', 'dblclick', 'contextmenu'].forEach(evtName => {
        window.addEventListener(evtName, (e) => {
            const target = e.target;
            const rect = target && target.getBoundingClientRect ? target.getBoundingClientRect() : { x: 0, y: 0, width: 0, height: 0 };
            
            const payload = {
                type: evtName,
                timestamp: Date.now(),
                pixel: {
                    x: Math.round(e.clientX),
                    y: Math.round(e.clientY),
                    pageX: Math.round(e.pageX),
                    pageY: Math.round(e.pageY),
                    offsetX: Math.round(e.offsetX),
                    offsetY: Math.round(e.offsetY)
                },
                element: {
                    tag: target ? target.tagName.toLowerCase() : '',
                    id: target ? target.id || '' : '',
                    className: target ? target.className || '' : '',
                    selector: getCssSelector(target),
                    text: (target ? (target.innerText || target.textContent || '') : '').trim().slice(0, 100),
                    value: target && 'value' in target ? String(target.value).slice(0, 100) : undefined,
                    rect: {
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height)
                    }
                }
            };

            if (window.__agentUserActionBinding) {
                try {
                    window.__agentUserActionBinding(JSON.stringify(payload));
                } catch(err) {}
            }
        }, true); // capture phase to intercept early
    });

    // Capture input change / form submit
    window.addEventListener('change', (e) => {
        const target = e.target;
        if (!target) return;
        const payload = {
            type: 'change',
            timestamp: Date.now(),
            element: {
                tag: target.tagName.toLowerCase(),
                id: target.id || '',
                selector: getCssSelector(target),
                value: String(target.value || '').slice(0, 150)
            }
        };
        if (window.__agentUserActionBinding) {
            try {
                window.__agentUserActionBinding(JSON.stringify(payload));
            } catch(err) {}
        }
    }, true);

    // Capture scroll depths (throttled)
    let lastScroll = 0;
    window.addEventListener('scroll', () => {
        const now = Date.now();
        if (now - lastScroll < 200) return;
        lastScroll = now;

        const maxScroll = (document.documentElement.scrollHeight - window.innerHeight) || 1;
        const currentScroll = window.scrollY || window.pageYOffset || 0;
        const percent = Math.min(100, Math.round((currentScroll / maxScroll) * 100));

        const payload = {
            type: 'scroll',
            timestamp: now,
            scrollY: Math.round(currentScroll),
            scrollPercent: percent
        };
        if (window.__agentUserActionBinding) {
            try {
                window.__agentUserActionBinding(JSON.stringify(payload));
            } catch(err) {}
        }
    }, { passive: true });
})();
"""

class UserTracker:
    """
    Real-time User Interaction Capture Engine.
    Listens to physical human actions (pixel clicks, inputs, scrolls) via in-page hooks and CDP bindings.
    """
    def __init__(self, cdp_conn, active_session_id_getter):
        self.cdp = cdp_conn
        self.get_session_id = active_session_id_getter
        self.action_history: List[Dict[str, Any]] = []
        self._max_history = 500
        self.binding_name = "__agentUserActionBinding"
        self._installed = False

    async def enable(self):
        """
        Installs the CDP binding and evaluates the script on new document creation.
        """
        sid = self.get_session_id()
        try:
            # 1. Add CDP binding
            await self.cdp.send("Runtime.addBinding", {"name": self.binding_name}, session_id=sid)
        except Exception:
            pass

        # 2. Add script to evaluate on new document
        try:
            await self.cdp.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": USER_TRACKER_INJECT_JS},
                session_id=sid
            )
        except Exception:
            pass

        # 3. Inject directly into current document
        await self.inject_tracker()
        self._installed = True

    async def inject_tracker(self):
        sid = self.get_session_id()
        await self.cdp.send(
            "Runtime.evaluate",
            {"expression": USER_TRACKER_INJECT_JS, "userGesture": True},
            session_id=sid
        )

    async def handle_cdp_event(self, data: Dict[str, Any]):
        method = data.get("method", "")
        params = data.get("params", {})

        if method == "Runtime.bindingCalled" and params.get("name") == self.binding_name:
            payload_str = params.get("payload", "{}")
            try:
                action_data = json.loads(payload_str)
                action_data["id"] = f"act_{len(self.action_history) + 1}_{int(time.time()*1000)}"
                self.action_history.append(action_data)
                if len(self.action_history) > self._max_history:
                    self.action_history.pop(0)
            except Exception:
                pass

    def get_actions(self, limit: int = 20, event_type: Optional[str] = None) -> List[Dict[str, Any]]:
        if not event_type:
            return self.action_history[-limit:]
        return [a for a in self.action_history if a.get("type") == event_type][-limit:]

    def get_last_action(self) -> Optional[Dict[str, Any]]:
        return self.action_history[-1] if self.action_history else None

    def clear(self):
        self.action_history.clear()
