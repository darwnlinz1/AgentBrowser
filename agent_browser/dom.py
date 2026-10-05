import json
from typing import Dict, Any, List, Optional, Tuple

INTERACTIVE_SNAPSHOT_JS = r"""
(() => {
    let index = 1;
    const elements = [];
    const walk = (node, framePath = "") => {
        if (!node || node.nodeType !== Node.ELEMENT_NODE) return;

        const tag = node.tagName.toLowerCase();
        if (['script', 'style', 'noscript', 'svg', 'path', 'meta', 'link'].includes(tag)) return;

        const rect = node.getBoundingClientRect();
        const style = window.getComputedStyle(node);
        const isVisible = style.display !== 'none' && 
                          style.visibility !== 'hidden' && 
                          parseFloat(style.opacity || '1') > 0.05 &&
                          rect.width > 0 && rect.height > 0;

        const isInteractive = ['button', 'input', 'select', 'textarea', 'a'].includes(tag) ||
                              node.getAttribute('role') === 'button' ||
                              node.getAttribute('role') === 'link' ||
                              node.getAttribute('contenteditable') === 'true' ||
                              node.hasAttribute('onclick') ||
                              style.cursor === 'pointer';

        if (isVisible && (isInteractive || ['h1','h2','h3','h4','p','span','div','li','table'].includes(tag))) {
            const text = (node.innerText || node.textContent || '').trim().replace(/\s+/g, ' ');
            const value = node.value !== undefined ? String(node.value) : undefined;
            const role = node.getAttribute('role') || tag;
            const ariaLabel = node.getAttribute('aria-label');
            const id = node.id || undefined;
            const name = node.getAttribute('name') || undefined;
            const href = node.getAttribute('href') || undefined;
            const placeholder = node.getAttribute('placeholder') || undefined;
            const type = node.getAttribute('type') || undefined;

            if (isInteractive || (text && text.length > 0 && text.length < 250)) {
                const elInfo = {
                    ref: index++,
                    tag: tag,
                    role: role,
                    text: text.slice(0, 150),
                    value: value,
                    id: id,
                    name: name,
                    type: type,
                    href: href,
                    placeholder: placeholder,
                    ariaLabel: ariaLabel,
                    interactive: isInteractive,
                    rect: {
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height)
                    },
                    frame: framePath || undefined
                };
                node.setAttribute('data-agent-ref', String(elInfo.ref));
                elements.push(elInfo);
            }
        }

        // Traverse child elements
        if (tag === 'iframe') {
            try {
                const doc = node.contentDocument || node.contentWindow.document;
                if (doc && doc.body) {
                    walk(doc.body, (framePath ? framePath + " > " : "") + (node.id || node.name || "iframe"));
                }
            } catch (e) {
                // Cross-origin iframe
            }
        } else {
            for (let child of node.children) {
                walk(child, framePath);
            }
        }
    };

    walk(document.body || document.documentElement);
    return {
        url: window.location.href,
        title: document.title,
        elementCount: elements.length,
        elements: elements
    };
})()
"""

class DOMEngine:
    """
    DOM & Semantic Element Inspector and Mutator for AI Agents.
    Provides structured snapshots, CSS/XPath element selection, attribute/HTML mutations,
    and coordinate-precise interactions.
    """
    def __init__(self, cdp_conn, active_session_id_getter):
        self.cdp = cdp_conn
        self.get_session_id = active_session_id_getter

    async def get_interactive_snapshot(self) -> Dict[str, Any]:
        """
        Extracts a clean, machine-readable accessibility and interactive snapshot
        optimized for AI agent consumption.
        """
        sid = self.get_session_id()
        res = await self.cdp.send(
            "Runtime.evaluate",
            {
                "expression": INTERACTIVE_SNAPSHOT_JS,
                "returnByValue": True,
                "awaitPromise": True
            },
            session_id=sid
        )
        return res.get("result", {}).get("value", {})

    async def format_agent_view(self) -> str:
        """
        Converts snapshot into a concise token-efficient string representation.
        """
        snap = await self.get_interactive_snapshot()
        lines = [
            f"Page: {snap.get('title', '')} | URL: {snap.get('url', '')}",
            "--------------------------------------------------"
        ]
        elements = snap.get("elements", [])
        for el in elements:
            if not el.get("interactive") and not el.get("text"):
                continue
            ref = el.get("ref")
            tag = el.get("tag")
            role = el.get("role")
            text = el.get("text")
            val = el.get("value")
            id_attr = f"#{el['id']}" if el.get("id") else ""
            type_attr = f"[{el['type']}]" if el.get("type") else ""
            ph = f" placeholder='{el['placeholder']}'" if el.get("placeholder") else ""
            interactive_marker = "[ACTIONABLE]" if el.get("interactive") else ""

            parts = [f"[#{ref}]", f"<{tag}{id_attr}{type_attr}{ph}>"]
            if val is not None and val != "":
                parts.append(f"value='{val}'")
            if text:
                parts.append(f"'{text}'")
            if interactive_marker:
                parts.append(interactive_marker)
            lines.append(" ".join(parts))

        return "\n".join(lines)

    async def query_selector(self, selector: str) -> Optional[Dict[str, Any]]:
        """
        Finds element matching CSS selector or ref (#1, [data-agent-ref='1']).
        Returns element details, bounding rect, and attributes.
        """
        sid = self.get_session_id()
        expr = f"""
        (() => {{
            let sel = {json.dumps(selector)};
            let el = null;
            if (sel.startsWith("#") && !isNaN(sel.slice(1))) {{
                el = document.querySelector(`[data-agent-ref='${{sel.slice(1)}}']`);
            }}
            if (!el) el = document.querySelector(sel);
            if (!el) return null;

            const rect = el.getBoundingClientRect();
            const attrs = {{}};
            for (let a of el.attributes) {{
                attrs[a.name] = a.value;
            }}

            return {{
                tag: el.tagName.toLowerCase(),
                id: el.id,
                className: el.className,
                text: (el.innerText || el.textContent || '').trim(),
                value: el.value,
                attributes: attrs,
                rect: {{
                    x: Math.round(rect.x),
                    y: Math.round(rect.y),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    top: Math.round(rect.top),
                    left: Math.round(rect.left)
                }},
                outerHTML: el.outerHTML.slice(0, 1000)
            }};
        }})()
        """
        res = await self.cdp.send(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True},
            session_id=sid
        )
        return res.get("result", {}).get("value")

    async def click(self, selector: str) -> Dict[str, Any]:
        """
        Clicks an element using its center coordinates or synthetic fallback.
        """
        sid = self.get_session_id()
        el = await self.query_selector(selector)
        if not el or not el.get("rect"):
            raise ValueError(f"Element '{selector}' not found or not rendered.")

        rect = el["rect"]
        cx = rect["x"] + rect["width"] / 2
        cy = rect["y"] + rect["height"] / 2

        # CDP Input.dispatchMouseEvent at center coordinate
        await self.cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved",
            "x": cx,
            "y": cy
        }, session_id=sid)
        await self.cdp.send("Input.dispatchMouseEvent", {
            "type": "mousePressed",
            "x": cx,
            "y": cy,
            "button": "left",
            "clickCount": 1
        }, session_id=sid)
        await self.cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased",
            "x": cx,
            "y": cy,
            "button": "left",
            "clickCount": 1
        }, session_id=sid)

        return {"clicked": True, "selector": selector, "coordinates": {"x": cx, "y": cy}}

    async def type_text(self, selector: str, text: str, clear_first: bool = True) -> Dict[str, Any]:
        """
        Types text into the target element with option to clear previous value.
        """
        sid = self.get_session_id()
        await self.click(selector)

        if clear_first:
            expr_clear = f"""
            (() => {{
                let sel = {json.dumps(selector)};
                let el = sel.startsWith("#") && !isNaN(sel.slice(1)) ?
                         document.querySelector(`[data-agent-ref='${{sel.slice(1)}}']`) :
                         document.querySelector(sel);
                if (el) {{
                    el.value = '';
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
            }})()
            """
            await self.cdp.send("Runtime.evaluate", {"expression": expr_clear}, session_id=sid)

        for ch in text:
            await self.cdp.send("Input.dispatchKeyEvent", {
                "type": "keyDown",
                "text": ch
            }, session_id=sid)
            await self.cdp.send("Input.dispatchKeyEvent", {
                "type": "keyUp"
            }, session_id=sid)

        return {"typed": text, "selector": selector}

    async def mutate_element(self, selector: str, action: str, **kwargs) -> Dict[str, Any]:
        """
        Modifies an element on the live page:
        actions:
          - 'set_attribute': key, value
          - 'remove_attribute': key
          - 'set_html': html
          - 'set_text': text
          - 'set_style': property, value
          - 'remove_node'
        """
        sid = self.get_session_id()
        expr = f"""
        (() => {{
            let sel = {json.dumps(selector)};
            let el = sel.startsWith("#") && !isNaN(sel.slice(1)) ?
                     document.querySelector(`[data-agent-ref='${{sel.slice(1)}}']`) :
                     document.querySelector(sel);
            if (!el) return {{ success: false, error: 'Element not found' }};

            const action = {json.dumps(action)};
            const kwargs = {json.dumps(kwargs)};

            if (action === 'set_attribute') {{
                el.setAttribute(kwargs.key, kwargs.value);
            }} else if (action === 'remove_attribute') {{
                el.removeAttribute(kwargs.key);
            }} else if (action === 'set_html') {{
                el.innerHTML = kwargs.html;
            }} else if (action === 'set_text') {{
                el.textContent = kwargs.text;
            }} else if (action === 'set_style') {{
                el.style[kwargs.property] = kwargs.value;
            }} else if (action === 'remove_node') {{
                el.remove();
                return {{ success: true, removed: true }};
            }} else {{
                return {{ success: false, error: 'Unsupported action: ' + action }};
            }}

            return {{
                success: true,
                tag: el.tagName.toLowerCase(),
                outerHTML: el.outerHTML.slice(0, 500)
            }};
        }})()
        """
        res = await self.cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session_id=sid)
        return res.get("result", {}).get("value", {})

    async def get_outer_html(self, selector: Optional[str] = None) -> str:
        """
        Retrieves full document HTML or specific element HTML.
        """
        sid = self.get_session_id()
        if selector:
            expr = f"""
            (() => {{
                let sel = {json.dumps(selector)};
                let el = sel.startsWith("#") && !isNaN(sel.slice(1)) ?
                         document.querySelector(`[data-agent-ref='${{sel.slice(1)}}']`) :
                         document.querySelector(sel);
                return el ? el.outerHTML : '';
            }})()
            """
        else:
            expr = "document.documentElement.outerHTML"

        res = await self.cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session_id=sid)
        return res.get("result", {}).get("value", "")
