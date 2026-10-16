import json
from typing import Dict, Any, List, Optional


class FocusEngine:
    """
    Directed Focus & Element Pinning System.
    Allows user or AI to pin specific subtrees/regions of interest to prevent reading full 50,000 token DOMs.
    """
    def __init__(self, cdp_conn, active_session_id_getter):
        self.cdp = cdp_conn
        self.get_session_id = active_session_id_getter
        self.pinned_selectors: Dict[str, str] = {}  # alias/name -> selector
        self.last_snapshots: Dict[str, str] = {}    # alias -> previous HTML sample
        self.last_lengths: Dict[str, int] = {}

    def pin(self, selector: str, alias: Optional[str] = None) -> str:
        name = alias or selector
        self.pinned_selectors[name] = selector
        return name

    def unpin(self, alias_or_selector: str) -> bool:
        if alias_or_selector in self.pinned_selectors:
            del self.pinned_selectors[alias_or_selector]
            self.last_snapshots.pop(alias_or_selector, None)
            self.last_lengths.pop(alias_or_selector, None)
            return True
        for k, v in list(self.pinned_selectors.items()):
            if v == alias_or_selector:
                del self.pinned_selectors[k]
                self.last_snapshots.pop(k, None)
                self.last_lengths.pop(k, None)
                return True
        return False

    def get_pinned(self) -> Dict[str, str]:
        return dict(self.pinned_selectors)

    def clear(self):
        self.pinned_selectors.clear()
        self.last_snapshots.clear()
        self.last_lengths.clear()

    async def get_focused_view(self) -> Dict[str, Any]:
        """
        Extracts only the content of pinned elements, showing clean text, inputs, and DOM deltas.
        """
        sid = self.get_session_id()
        if not self.pinned_selectors:
            return {
                "pinned_count": 0,
                "message": "No elements pinned yet. Use 'pin <selector>' to focus on specific regions.",
                "regions": {}
            }

        script = f"""
        (() => {{
            const pins = {json.dumps(self.pinned_selectors)};
            const results = {{}};
            for (let [alias, sel] of Object.entries(pins)) {{
                const el = document.querySelector(sel);
                if (!el) {{
                    results[alias] = {{ found: false, selector: sel }};
                    continue;
                }}

                const rect = el.getBoundingClientRect();
                const inputs = [];
                el.querySelectorAll('input, select, textarea').forEach(inp => {{
                    inputs.push({{
                        name: inp.name || inp.id || inp.getAttribute('placeholder') || '',
                        type: inp.type || '',
                        value: inp.value || ''
                    }});
                }});

                results[alias] = {{
                    found: true,
                    selector: sel,
                    tag: el.tagName.toLowerCase(),
                    text: (el.innerText || el.textContent || '').trim().slice(0, 800),
                    inputs: inputs,
                    rect: {{
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height)
                    }},
                    outerHTML: el.outerHTML.slice(0, 8000),
                    outerHTMLLength: el.outerHTML.length
                }};
            }}
            return results;
        }})()
        """
        res = await self.cdp.send("Runtime.evaluate", {"expression": script, "returnByValue": True}, session_id=sid)
        regions_data = res.get("result", {}).get("value", {})

        # Compute simple deltas
        deltas = {}
        for alias, info in regions_data.items():
            if info.get("found"):
                curr_html = info.get("outerHTML", "")
                full_len = info.get("outerHTMLLength", len(curr_html))
                prev_html = self.last_snapshots.get(alias, "")
                prev_len = self.last_lengths.get(alias)
                has_changed = (curr_html != prev_html) if prev_html else False
                if prev_len is not None and prev_len != full_len:
                    has_changed = True
                deltas[alias] = {
                    "changed": has_changed,
                    "previous_length": prev_len if prev_len is not None else len(prev_html),
                    "current_length": full_len,
                    "outerHTMLLength": full_len,
                }
                self.last_lengths[alias] = full_len
                self.last_snapshots[alias] = curr_html

        return {
            "pinned_count": len(self.pinned_selectors),
            "regions": regions_data,
            "deltas": deltas
        }
