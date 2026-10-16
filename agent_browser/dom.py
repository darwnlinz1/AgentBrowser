import json
from typing import Dict, Any, List, Optional, Tuple

from agent_browser.axtree import (
    build_ref_table,
    diff_refs,
    find_reanchor,
    merkle_hashes,
    ref_freshness,
    render_budgeted_view,
    render_view,
    summarize_partial,
)
from agent_browser.fingerprints import ElementFingerprint, resolve_fingerprint

INTERACTIVE_SNAPSHOT_JS = r"""
(() => {
    let index = 1;
    const elements = [];
    // Clear refs from a previous snapshot first: a stale data-agent-ref on a
    // now-hidden element could otherwise make '#N' resolve to the wrong node.
    document.querySelectorAll('[data-agent-ref]').forEach(e => e.removeAttribute('data-agent-ref'));
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
            const __rawType = (node.getAttribute('type') || '').toLowerCase();
            const __nameHint = (node.getAttribute('name') || '') + ' ' + (node.getAttribute('autocomplete') || '') + ' ' + (node.id || '');
            const __sensitive = __rawType === 'password' || /(otp|one[-_]?time|cvv|cvc|cc[-_]?number|card[-_]?number|security[-_]?code)/i.test(__nameHint);
            const value = node.value !== undefined ? (__sensitive ? '[redacted]' : String(node.value)) : undefined;
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
//# sourceURL=__agent_internal_scanner__.js
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
        self._ax_refs: List[Dict[str, Any]] = []
        self._ax_hashes: Dict[Any, str] = {}
        # Self-healing: last known fingerprint per selector + heal audit log.
        self._fingerprints: Dict[str, ElementFingerprint] = {}
        self.heal_log: List[Dict[str, Any]] = []
        # Which snapshot minted the refs the agent last saw ('ax' | 'legacy').
        # Numeric #ref clicks route to the table that issued them.
        self._last_view_source: Optional[str] = None

    async def get_axtree_snapshot(self, include_text: bool = False) -> Dict[str, Any]:
        """
        Node-identity snapshot from the Chromium accessibility tree.

        Refs bind to ``backendDOMNodeId`` in a Python-side table; nothing is
        written into the page DOM. Includes a Merkle root over the AX tree so
        callers can detect *whether* anything changed with one hash compare,
        and ``diff_refs`` can then return only the changed subtree.
        """
        sid = self.get_session_id()
        try:
            await self.cdp.send("Accessibility.enable", {}, session_id=sid)
        except Exception:
            pass
        res = await self.cdp.send("Accessibility.getFullAXTree", {}, session_id=sid)
        self._last_view_source = "ax"
        nodes = res.get("nodes") or []
        previous = self._ax_refs
        refs = build_ref_table(nodes, include_text=include_text)
        self._ax_refs = refs
        self._ax_hashes = merkle_hashes(nodes)
        root_hash = None
        if nodes:
            root_ids = [n.get("nodeId") for n in nodes if n.get("parentId") is None]
            if root_ids:
                root_hash = self._ax_hashes.get(root_ids[0])
        return {
            "refs": refs,
            "count": len(refs),
            "merkle_root": root_hash,
            "node_hashes": {str(k): v for k, v in self._ax_hashes.items()},
            "diff_from_previous": diff_refs(previous, refs) if previous else None,
        }

    async def format_axtree_view(self, include_text: bool = False, budget_tokens: int = 400) -> str:
        snap = await self.get_axtree_snapshot(include_text=include_text)
        # Title/URL via a cheap evaluate; failure is non-fatal.
        title = url = ""
        try:
            sid = self.get_session_id()
            res = await self.cdp.send("Runtime.evaluate", {
                "expression": "({t: document.title, u: location.href})", "returnByValue": True,
            }, session_id=sid, timeout=3.0)
            val = (res.get("result") or {}).get("value") or {}
            title, url = val.get("t", ""), val.get("u", "")
        except Exception:
            pass
        return render_budgeted_view(snap["refs"], title=title, url=url, budget_tokens=budget_tokens)

    async def _partial_ax_summary(self, backend_id: Any, sid: Optional[str]) -> Optional[Dict[str, Any]]:
        """Current live AX summary for one backend node, or None if unavailable."""
        try:
            res = await self.cdp.send(
                "Accessibility.getPartialAXTree",
                {"backendNodeId": backend_id, "fetchRelatives": False},
                session_id=sid,
            )
            return summarize_partial(res.get("nodes") or [])
        except Exception:
            return None

    async def resolve_ax_ref(self, ref: int) -> Optional[Dict[str, Any]]:
        """
        Resolve a [#ref] from the last AX snapshot to its live node info.

        The stored fingerprint is revalidated first: if role/name changed,
        the ref is stale. A unique fresh ref with the same identity becomes
        the reanchor target; zero or multiple matches fail closed.
        """
        entry = next((r for r in self._ax_refs if r.get("ref") == ref), None)
        if not entry:
            return None
        sid = self.get_session_id()
        try:
            live = await self._partial_ax_summary(entry["backend_id"], sid)
            freshness = ref_freshness(entry, live)
            effective = entry
            reanchored = None
            if freshness["identity_changed"]:
                snap = await self.get_axtree_snapshot()
                found = find_reanchor(entry, snap.get("refs") or [])
                if not found["candidate"]:
                    return {
                        **entry,
                        "resolved": False,
                        "stale": True,
                        "freshness": freshness,
                        "reanchor_candidate_count": found["candidate_count"],
                        "error": "stale AX ref: role/name changed and no unique reanchor exists",
                    }
                effective = found["candidate"]
                reanchored = {
                    "from_backend_id": entry.get("backend_id"),
                    "to_backend_id": effective.get("backend_id"),
                    "to_ref": effective.get("ref"),
                }

            resolved = await self.cdp.send(
                "DOM.resolveNode", {"backendNodeId": effective["backend_id"]}, session_id=sid
            )
            object_id = ((resolved.get("object") or {}).get("objectId"))
            if not object_id and not reanchored:
                # Node is gone (remounted). One reanchor attempt by identity.
                snap = await self.get_axtree_snapshot()
                found = find_reanchor(entry, snap.get("refs") or [])
                candidate = found["candidate"]
                if candidate and candidate.get("backend_id") != entry.get("backend_id"):
                    effective = candidate
                    reanchored = {
                        "from_backend_id": entry.get("backend_id"),
                        "to_backend_id": effective.get("backend_id"),
                        "to_ref": effective.get("ref"),
                        "reason": "node_gone",
                    }
                    resolved = await self.cdp.send(
                        "DOM.resolveNode", {"backendNodeId": effective["backend_id"]}, session_id=sid
                    )
                    object_id = ((resolved.get("object") or {}).get("objectId"))
            if not object_id:
                return {**effective, "resolved": False, "freshness": freshness, "reanchored": reanchored}
            described = await self.cdp.send("Runtime.callFunctionOn", {
                "objectId": object_id,
                "functionDeclaration": "function(){ const r=this.getBoundingClientRect(); return {tag:this.tagName.toLowerCase(), text:(this.innerText||this.textContent||'').trim().slice(0,120), rect:{x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height)}}; }",
                "returnByValue": True,
            }, session_id=sid)
            info = (((described.get("result") or {}).get("value")) or {})
            return {
                **effective,
                "resolved": True,
                "object_id": object_id,
                "freshness": freshness,
                "reanchored": reanchored,
                **info,
            }
        except Exception as exc:
            return {**entry, "resolved": False, "error": str(exc)}

    async def resolve_healed_ref(self, selector: str) -> Optional[Dict[str, Any]]:
        """
        Re-locate an element whose selector broke, by fingerprint similarity
        over a fresh snapshot. Returns {ref, confidence, evidence} or None
        (fail closed: never click a below-threshold guess). Every outcome is
        appended to ``heal_log`` as evidence.
        """
        stored = self._fingerprints.get(selector)
        if stored is None:
            return None
        snap = await self.get_interactive_snapshot()
        elements = snap.get("elements") or []
        candidates = [ElementFingerprint.from_snapshot_element(el) for el in elements]
        result = resolve_fingerprint(stored, candidates)
        self.heal_log.append({"selector": selector, **result["evidence"]})
        if not result["healed"]:
            return None
        match = result["match"]
        idx = candidates.index(match)
        return {
            "ref": elements[idx].get("ref"),
            "confidence": result["confidence"],
            "evidence": result["evidence"],
        }

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
        self._last_view_source = "legacy"
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
        value = res.get("result", {}).get("value")
        if value and not (selector.startswith("#") and selector[1:].isdigit()):
            # Remember what this selector pointed at, so a later break can heal.
            try:
                self._fingerprints[selector] = ElementFingerprint.from_snapshot_element(value)
            except Exception:
                pass
        return value

    async def click_ax_ref(self, ref: int) -> Dict[str, Any]:
        """Click an accessibility-tree ref by node identity (no page writes)."""
        entry = next((r for r in self._ax_refs if r.get("ref") == ref), None)
        if not entry:
            raise ValueError(f"AX ref #{ref} not in the last accessibility snapshot.")
        sid = self.get_session_id()
        checked = await self.resolve_ax_ref(ref)
        if not checked or not checked.get("resolved"):
            reason = (checked or {}).get("error") or "stale or unresolved AX ref"
            raise ValueError(f"AX ref #{ref} could not be clicked: {reason}")
        entry = checked
        object_id = checked.get("object_id")
        if not object_id:
            raise ValueError(f"AX ref #{ref} could not be resolved to a live node.")
        res = await self.cdp.send("Runtime.callFunctionOn", {
            "objectId": object_id,
            "functionDeclaration": (
                "function(){ this.scrollIntoView({block:'center', inline:'center'});"
                " const r = this.getBoundingClientRect();"
                " const cx = r.left + r.width/2, cy = r.top + r.height/2;"
                " const hit = document.elementFromPoint(cx, cy) || this;"
                " const t = this.contains(hit) ? hit : this;"
                " const init = {bubbles:true, cancelable:true, view:window, clientX:cx, clientY:cy, button:0};"
                " t.dispatchEvent(new PointerEvent('pointerdown', init));"
                " t.dispatchEvent(new MouseEvent('mousedown', init));"
                " t.dispatchEvent(new PointerEvent('pointerup', init));"
                " t.dispatchEvent(new MouseEvent('mouseup', init));"
                " t.dispatchEvent(new MouseEvent('click', init));"
                " return {clicked:true, x:cx, y:cy, tag:this.tagName.toLowerCase(),"
                " text:(this.innerText||this.textContent||'').trim().slice(0,80)}; }"
            ),
            "returnByValue": True,
        }, session_id=sid)
        value = ((res.get("result") or {}).get("value") or {})
        if not value.get("clicked"):
            raise ValueError(f"AX ref #{ref} could not be clicked.")
        return {
            "clicked": True,
            "selector": f"#{ref}",
            "via": "axtree",
            "coordinates": {"x": value.get("x"), "y": value.get("y")},
            "element": {"tag": value.get("tag"), "id": entry.get("name"), "text": value.get("text")},
            "freshness": entry.get("freshness"),
            "reanchored": entry.get("reanchored"),
        }

    async def click(self, selector: str, action_tag: Optional[str] = None) -> Dict[str, Any]:
        """
        Click the element so its listeners actually run.

        Input.dispatchMouseEvent is not used here. Coordinate input misses the
        target when the CSS viewport and the CDP input surface disagree. The
        click is a MouseEvent at the element's center, which fires the same
        listeners a user click does and records those coordinates.
        """
        sid = self.get_session_id()
        # Numeric refs route to the snapshot that minted them: AX refs by
        # node identity when the last view was the accessibility tree.
        if selector.startswith("#") and selector[1:].isdigit() and self._last_view_source == "ax":
            try:
                return await self.click_ax_ref(int(selector[1:]))
            except Exception:
                pass  # fall through to the legacy path below
        el = await self.query_selector(selector)
        if not el or not el.get("rect"):
            # Last resort: an AX ref from an earlier accessibility view.
            if selector.startswith("#") and selector[1:].isdigit() and self._ax_refs:
                return await self.click_ax_ref(int(selector[1:]))
            raise ValueError(f"Element '{selector}' not found or not rendered.")

        rect = el["rect"]
        cx = rect["x"] + rect["width"] / 2
        cy = rect["y"] + rect["height"] / 2
        expr = f"""
        (() => {{
            let sel = {json.dumps(selector)};
            let el = null;
            if (sel.startsWith("#") && !isNaN(sel.slice(1))) {{
                el = document.querySelector(`[data-agent-ref='${{sel.slice(1)}}']`);
            }}
            if (!el) el = document.querySelector(sel);
            if (!el) return {{ clicked: false, reason: "not_found" }};
            el.scrollIntoView({{block: "center", inline: "center"}});
            const rect = el.getBoundingClientRect();
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            const hit = document.elementFromPoint(cx, cy) || el;
            const target = el.contains(hit) ? hit : el;
            const init = {{
                bubbles: true,
                cancelable: true,
                view: window,
                clientX: cx,
                clientY: cy,
                button: 0
            }};
            // MouseEvent carries the center coordinates. HTMLElement.click()
            // does not, so the user tracker would record 0,0.
            target.dispatchEvent(new PointerEvent("pointerdown", init));
            target.dispatchEvent(new MouseEvent("mousedown", init));
            target.dispatchEvent(new PointerEvent("pointerup", init));
            target.dispatchEvent(new MouseEvent("mouseup", init));
            target.dispatchEvent(new MouseEvent("click", init));
            return {{
                clicked: true,
                x: cx,
                y: cy,
                tag: el.tagName.toLowerCase(),
                id: el.id || "",
                text: (el.innerText || el.textContent || "").trim().slice(0, 80)
            }};
        }})()
        """
        if action_tag:
            # Stamp the dispatch script so any request/exception whose async
            # initiator chain contains this sourceURL is provably caused by it.
            expr = expr + f"\n//# sourceURL={action_tag}.js"
        res = await self.cdp.send(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "userGesture": True},
            session_id=sid,
        )
        value = (res.get("result") or {}).get("value") or {}
        if not value.get("clicked"):
            raise ValueError(f"Element '{selector}' could not be clicked: {value.get('reason')}")
        return {
            "clicked": True,
            "selector": selector,
            "coordinates": {"x": value.get("x", cx), "y": value.get("y", cy)},
            "element": {"tag": value.get("tag"), "id": value.get("id"), "text": value.get("text")},
        }

    async def type_text(self, selector: str, text: str, clear_first: bool = True, action_tag: Optional[str] = None) -> Dict[str, Any]:
        """
        Types text into the target element with option to clear previous value.

        Focus is moved explicitly first: synthetic mouse events are untrusted
        and do not move focus, so keystrokes could otherwise land in whatever
        element happened to be focused. Text is inserted with the trusted
        ``Input.insertText`` path, which fires the same input events a real
        keyboard produces (including on React-controlled inputs).
        """
        sid = self.get_session_id()
        await self.click(selector, action_tag=action_tag)

        # Explicit focus + optional clear via the native value setter so
        # framework-controlled (React) inputs register the change.
        # When an action_tag is present, insertion is attempted inside this
        # stamped script via execCommand: the input handler (and any timer or
        # fetch it schedules) then carries the action's sourceURL in its async
        # initiator chain, so downstream effects are *attributed*, not merely
        # temporal. Trusted Input.insertText remains the fallback.
        expr_focus = f"""
        (() => {{
            let sel = {json.dumps(selector)};
            let el = sel.startsWith("#") && !isNaN(sel.slice(1)) ?
                     document.querySelector(`[data-agent-ref='${{sel.slice(1)}}']`) :
                     document.querySelector(sel);
            if (!el) return {{ focused: false, inserted: false }};
            el.focus();
            if ({json.dumps(bool(clear_first))}) {{
                const proto = el.constructor && el.constructor.prototype;
                const setter = proto && Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(el, ''); else el.value = '';
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            }}
            let inserted = false;
            const text = {json.dumps(text)};
            if (text) {{
                try {{ inserted = document.execCommand('insertText', false, text); }} catch (e) {{ inserted = false; }}
            }}
            return {{ focused: document.activeElement === el, inserted: !!inserted }};
        }})()
        """
        if action_tag:
            expr_focus = expr_focus + f"\n//# sourceURL={action_tag}.js"
        focus_res = await self.cdp.send("Runtime.evaluate", {"expression": expr_focus, "returnByValue": True}, session_id=sid)
        focus_val = ((focus_res.get("result") or {}).get("value") or {})

        if text and not focus_val.get("inserted"):
            await self.cdp.send("Input.insertText", {"text": text}, session_id=sid)

        return {"typed": text, "selector": selector, "focused": bool(focus_val.get("focused", True)),
                "inserted_via": "stamped_eval" if focus_val.get("inserted") else "input_insertText"}

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

    async def get_page_html(self) -> str:
        """
        Retrieves complete HTML source of the active document including DOCTYPE.
        """
        sid = self.get_session_id()
        expr = """
        (() => {
            const dt = document.doctype;
            const doctypeStr = dt ? `<!DOCTYPE ${dt.name}${dt.publicId ? ` PUBLIC "${dt.publicId}"` : ''}${(!dt.publicId && dt.systemId) ? ' SYSTEM' : ''}${dt.systemId ? ` "${dt.systemId}"` : ''}>\n` : '';
            return doctypeStr + (document.documentElement ? document.documentElement.outerHTML : '');
        })()
        //# sourceURL=__agent_internal_doctype__.js
        """
        try:
            res = await self.cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session_id=sid)
            val = res.get("result", {}).get("value")
            if val:
                return val
        except Exception:
            pass

        # Fallback to CDP DOM domain. Address the root by backendNodeId:
        # a depth:-1 getDocument both fetches the whole tree and invalidates
        # frontend nodeIds other components may be holding.
        try:
            doc_res = await self.cdp.send("DOM.getDocument", {"depth": 0}, session_id=sid)
            root = doc_res.get("root", {})
            backend_id = root.get("backendNodeId")
            if backend_id:
                html_res = await self.cdp.send("DOM.getOuterHTML", {"backendNodeId": backend_id}, session_id=sid)
                return html_res.get("outerHTML") or ""
        except Exception:
            pass
        return ""

    async def get_outer_html(self, selector: Optional[str] = None) -> str:
        """
        Retrieves full document HTML or specific element HTML.
        """
        if not selector:
            return await self.get_page_html()

        sid = self.get_session_id()
        expr = f"""
        (() => {{
            let sel = {json.dumps(selector)};
            let el = sel.startsWith("#") && !isNaN(sel.slice(1)) ?
                     document.querySelector(`[data-agent-ref='${{sel.slice(1)}}']`) :
                     document.querySelector(sel);
            return el ? el.outerHTML : '';
        }})()
        //# sourceURL=__agent_internal_outer_html__.js
        """
        try:
            res = await self.cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session_id=sid)
            return res.get("result", {}).get("value", "")
        except Exception:
            return ""
