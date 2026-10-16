import os
import re
import json
import base64
import asyncio
from typing import Dict, Any, List, Optional, Callable


class SourceInspector:
    """
    Client-side Source Inspector, Script Harvester, and Crypto/Hooking Engine for AI Agents.
    Captures all loaded scripts (inline and external), sourcemaps, initiator call stacks,
    and installs early hooks into window.fetch, XMLHttpRequest, JSON.stringify, and crypto.subtle.
    """
    def __init__(self, cdp_conn, active_session_id_getter):
        self.cdp = cdp_conn
        self.get_session_id = active_session_id_getter
        # scriptId -> metadata + source
        self.scripts: Dict[str, Dict[str, Any]] = {}
        # url -> scriptId
        self.url_to_script_id: Dict[str, str] = {}
        self._debugger_enabled_for: set = set()
        self._reenable_task: Optional[asyncio.Task] = None
        # In-page intercepted crypto / plaintext events
        self.crypto_events: List[Dict[str, Any]] = []
        self._max_events = 1000

    async def enable_for_session(self, sid: Optional[str]):
        """Child-session enable: script harvesting only, no page-world hooks."""
        await self._enable_debugger(sid)

    async def enable(self, session_id=None):
        """
        Enables Debugger and Page domain, extends async call stack depth to 32,
        and registers early script hooking to capture plaintext and keys before encryption.
        """
        sid = session_id or self.get_session_id()
        await self._enable_debugger(sid)

        # 2. Inject early hooks on every new document (runs before page scripts)
        hook_js = """
        (function() {
            if (window.__agent_crypto_hooked__) return;
            window.__agent_crypto_hooked__ = true;
            window.__agent_crypto_logs__ = window.__agent_crypto_logs__ || [];

            function logEvent(kind, details) {
                const item = {
                    kind: kind,
                    timestamp: Date.now(),
                    details: details,
                    stack: (new Error()).stack
                };
                window.__agent_crypto_logs__.push(item);
                if (window.__agent_crypto_logs__.length > 500) {
                    window.__agent_crypto_logs__.shift();
                }
            }

            // Hook window.fetch
            const origFetch = window.fetch;
            if (origFetch) {
                window.fetch = async function(...args) {
                    let url = args[0];
                    if (typeof url === 'object' && url.url) url = url.url;
                    let init = args[1] || {};
                    let body = init.body;
                    let bodyPreview = typeof body === 'string' ? body : (body ? '[Non-string Body]' : null);
                    logEvent('fetch', {
                        url: String(url),
                        method: init.method || 'GET',
                        headers: init.headers || {},
                        body: bodyPreview
                    });
                    const pending = origFetch.apply(this, args);
                    try {
                        const response = await pending;
                        const clone = response.clone();
                        clone.text().then((text) => {
                            logEvent("fetch_response", {
                                url: String(url),
                                status: response.status,
                                body: text.length > 4000 ? text.slice(0, 4000) : text
                            });
                        }).catch(() => {});
                        return response;
                    } catch (err) {
                        return pending;
                    }
                };
            }

            // Hook XMLHttpRequest.prototype.send
            const origXhrSend = XMLHttpRequest.prototype.send;
            const origXhrOpen = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                this.__agent_method = method;
                this.__agent_url = url;
                return origXhrOpen.apply(this, [method, url, ...rest]);
            };
            XMLHttpRequest.prototype.send = function(body) {
                logEvent('xhr', {
                    url: String(this.__agent_url || ''),
                    method: this.__agent_method || 'GET',
                    body: typeof body === 'string' ? body : (body ? '[Non-string Body]' : null)
                });
                return origXhrSend.apply(this, [body]);
            };

            // Hook JSON.stringify. Also record a string argument when the caller
            // is an encrypt* function, so plaintext is captured before the cipher.
            const origStringify = JSON.stringify;
            JSON.stringify = function(val, replacer, space) {
                try {
                    const caller = (new Error()).stack || "";
                    if (typeof val === "string" && val.length > 0 && val.length < 50000 && /encrypt/i.test(caller)) {
                        logEvent("pre_encrypt", { plaintext: val, via: "encrypt-frame" });
                    }
                    if (val && typeof val === 'object') {
                        // Capture potential payload objects before stringification/encryption
                        const jsonStr = origStringify(val);
                        if (jsonStr.length > 2 && jsonStr.length < 50000) {
                            logEvent('json_stringify', {
                                preview: jsonStr.length > 500 ? jsonStr.substring(0, 500) + '...' : jsonStr,
                                keys: Object.keys(val).slice(0, 20)
                            });
                        }
                    }
                } catch(e) {}
                return origStringify.apply(this, [val, replacer, space]);
            };

            // Hook Web Cryptography API if available
            if (window.crypto && window.crypto.subtle) {
                const subtle = window.crypto.subtle;
                const origEncrypt = subtle.encrypt;
                if (origEncrypt) {
                    subtle.encrypt = async function(algo, key, data) {
                        try {
                            logEvent('crypto_subtle_encrypt', {
                                algorithm: typeof algo === 'string' ? algo : algo.name,
                                dataLength: data ? (data.byteLength || data.length) : 0
                            });
                        } catch(e) {}
                        return origEncrypt.apply(this, [algo, key, data]);
                    };
                }
                const origDecrypt = subtle.decrypt;
                if (origDecrypt) {
                    subtle.decrypt = async function(algo, key, data) {
                        try {
                            logEvent('crypto_subtle_decrypt', {
                                algorithm: typeof algo === 'string' ? algo : algo.name,
                                dataLength: data ? (data.byteLength || data.length) : 0
                            });
                        } catch(e) {}
                        return origDecrypt.apply(this, [algo, key, data]);
                    };
                }
            }
        })();
        //# sourceURL=__agent_internal_hooks__.js
        """
        try:
            await self.cdp.send("Page.addScriptToEvaluateOnNewDocument", {
                "source": hook_js
            }, session_id=sid)
        except Exception:
            pass

    async def _refresh_debugger(self, sid: Optional[str]):
        """
        Drop and re-enable Debugger after a top-frame navigation.

        Debugger.enable alone does not recover scripts whose scriptParsed
        events were already consumed and then cleared from the table.
        """
        if not sid:
            return
        try:
            await self.cdp.send("Debugger.disable", {}, session_id=sid)
        except Exception:
            pass
        await self._enable_debugger(sid)

    async def _enable_debugger(self, sid: Optional[str]):
        """
        Enable Debugger so scriptParsed fires for the current document.

        Calling this again after a navigation re-emits scriptParsed for
        scripts that were parsed before the previous enable, which is the
        only way those script ids survive a frameNavigated clear.
        """
        if not sid:
            return
        try:
            await self.cdp.send("Debugger.enable", {}, session_id=sid)
            await self.cdp.send("Debugger.setAsyncCallStackDepth", {"maxDepth": 32}, session_id=sid)
            # Harvest mode never pauses: a `debugger;` statement or anti-debug
            # loop in page code must not freeze the page being inspected.
            try:
                await self.cdp.send("Debugger.setSkipAllPauses", {"skip": True}, session_id=sid)
            except Exception:
                pass
            self._debugger_enabled_for.add(sid)
        except Exception:
            pass

    async def handle_cdp_event(self, data: Dict[str, Any]):
        method = data.get("method", "")
        params = data.get("params", {})

        if method == "Debugger.scriptParsed":
            script_id = params.get("scriptId")
            url = params.get("url", "")
            source_map_url = params.get("sourceMapURL", "")
            has_source_url = params.get("hasSourceURL", False)
            start_line = params.get("startLine", 0)
            end_line = params.get("endLine", 0)

            # Skip internal agent scripts and helper evaluations
            if url and ("__agent_internal_" in url or url.startswith("__agent_")):
                return

            script_info = {
                "scriptId": script_id,
                "url": url if url else f"[inline-or-eval-{script_id}]",
                "is_inline": not bool(url),
                "sourceMapURL": source_map_url,
                "lines": end_line - start_line + 1,
                "length": params.get("length", 0),
                "hash": params.get("hash", ""),
                "content": None  # loaded on demand or cached
            }
            self.scripts[script_id] = script_info
            if url:
                self.url_to_script_id[url] = script_id

        elif method == "Page.frameNavigated":
            # A child frame must not wipe the top-frame script table.
            # A top-frame navigation drops scripts that belonged to the previous document.
            frame = params.get("frame", {})
            if frame.get("parentId"):
                return
            self.scripts.clear()
            self.url_to_script_id.clear()
            sid = data.get("sessionId") or self.get_session_id()
            # Drop the session from the enabled set so the re-enable is not a no-op.
            self._debugger_enabled_for.discard(sid)
            # scriptParsed events that arrived before this clear are gone.
            # Disable then enable so Chrome emits scriptParsed for the new document.
            # get_script_source awaits this task before it looks up an id.
            self._reenable_task = asyncio.create_task(self._refresh_debugger(sid))

    async def get_script_source(self, script_id: str) -> Optional[str]:
        """
        Fetches the complete script source code using CDP Debugger.getScriptSource.
        Caches it in memory for instant reuse.
        """
        pending = self._reenable_task
        if pending is not None and not pending.done():
            try:
                await pending
            except Exception:
                pass
        info = self.scripts.get(script_id)
        if not info:
            # The id may have been issued before the table was cleared.
            # One more Debugger.enable asks Chrome to re-emit scriptParsed.
            await self._enable_debugger(self.get_session_id())
            info = self.scripts.get(script_id)
        if not info:
            return None
        if info.get("content") is not None:
            return info["content"]

        sid = self.get_session_id()
        try:
            res = await self.cdp.send("Debugger.getScriptSource", {"scriptId": script_id}, session_id=sid)
            code = res.get("scriptSource", "")
            info["content"] = code
            return code
        except Exception as e:
            return f"[Error fetching script {script_id}: {str(e)}]"

    async def list_all_scripts(self, include_inline: bool = True) -> List[Dict[str, Any]]:
        """
        Returns list of all scripts loaded by the page.
        """
        results = []
        for s_id, s_info in self.scripts.items():
            if not include_inline and s_info["is_inline"]:
                continue
            results.append({
                "scriptId": s_id,
                "url": s_info["url"],
                "is_inline": s_info["is_inline"],
                "lines": s_info["lines"],
                "sourceMapURL": s_info["sourceMapURL"]
            })
        return results

    async def find_crypto_and_api_functions(self, script_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Scans script sources for cryptographic keywords, hashing routines, and API endpoints.
        """
        target_scripts = [script_id] if script_id else list(self.scripts.keys())
        findings = []

        crypto_patterns = [
            (r"(CryptoJS|aes|rsa|encrypt|decrypt|sign|digest|hmac|md5|sha256|sha1|base64)", "crypto_routine"),
            (r"(fetch\(|axios|\.ajax|XMLHttpRequest|\.post\(|\.get\()", "network_call"),
            (r"(api/v\d+|/api/|/auth/|/login|/token|/gateway)", "api_endpoint"),
            (r"(JSON\.stringify|JSON\.parse)", "serialization"),
            (r"(atob\(|btoa\(|decodeURIComponent)", "encoding_transform")
        ]

        for s_id in target_scripts:
            code = await self.get_script_source(s_id)
            if not code or code.startswith("[Error"):
                continue

            lines = code.splitlines()
            s_url = self.scripts.get(s_id, {}).get("url", s_id)

            for line_idx, line in enumerate(lines):
                for regex_str, tag in crypto_patterns:
                    matches = re.finditer(regex_str, line, re.IGNORECASE)
                    for m in matches:
                        # Extract surrounding snippet (±3 lines)
                        start = max(0, line_idx - 2)
                        end = min(len(lines), line_idx + 3)
                        snippet = "\n".join(lines[start:end])
                        findings.append({
                            "scriptId": s_id,
                            "url": s_url,
                            "line": line_idx + 1,
                            "match": m.group(0),
                            "tag": tag,
                            "snippet": snippet
                        })
                        if len(findings) >= 50:
                            return findings
        return findings

    async def get_caller_context_for_request(self, initiator: Dict[str, Any]) -> Dict[str, Any]:
        """
        Resolves the exact script code lines and function responsible for triggering an API request.
        """
        init_type = initiator.get("type", "other")
        stack = initiator.get("stack", {})
        frames = stack.get("callFrames", [])

        if not frames:
            return {
                "type": init_type,
                "has_stack": False,
                "frames": []
            }

        resolved_frames = []
        # Skip the agent's own fetch wrapper so the named caller is the page script.
        page_frames = [
            f for f in frames
            if "__agent_internal_" not in (f.get("url") or "")
            and not (f.get("url") or "").startswith("__agent_")
        ] or frames
        for f in page_frames[:5]:
            f_script_id = f.get("scriptId")
            f_url = f.get("url")
            f_func = f.get("functionName") or "(anonymous)"
            f_line = f.get("lineNumber", 0)
            f_col = f.get("columnNumber", 0)

            code_snippet = None
            if f_script_id:
                full_source = await self.get_script_source(f_script_id)
                if full_source and not full_source.startswith("[Error"):
                    lines = full_source.splitlines()
                    if 0 <= f_line < len(lines):
                        s_start = max(0, f_line - 3)
                        s_end = min(len(lines), f_line + 4)
                        code_snippet = "\n".join(
                            f"{'>' if i == f_line else ' '} {i+1}: {lines[i]}"
                            for i in range(s_start, s_end)
                        )

            resolved_frames.append({
                "function": f_func,
                "scriptId": f_script_id,
                "url": f_url,
                "line": f_line + 1,
                "column": f_col + 1,
                "code_snippet": code_snippet
            })

        return {
            "type": init_type,
            "has_stack": True,
            "top_caller": resolved_frames[0] if resolved_frames else None,
            "call_stack": resolved_frames
        }

    async def collect_crypto_hook_logs(self) -> List[Dict[str, Any]]:
        """
        Extracts intercepted plaintext and cryptographic logs captured in the page context.
        """
        sid = self.get_session_id()
        try:
            res = await self.cdp.send("Runtime.evaluate", {
                "expression": "JSON.stringify(window.__agent_crypto_logs__ || [])",
                "returnByValue": True
            }, session_id=sid)
            raw_json = res.get("result", {}).get("value", "[]")
            parsed = json.loads(raw_json)
            return parsed
        except Exception:
            return []

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

        # Fallback to CDP DOM domain
        try:
            doc_res = await self.cdp.send("DOM.getDocument", {"depth": -1}, session_id=sid)
            node_id = doc_res.get("root", {}).get("nodeId")
            if node_id:
                html_res = await self.cdp.send("DOM.getOuterHTML", {"nodeId": node_id}, session_id=sid)
                return html_res.get("outerHTML") or ""
        except Exception:
            pass
        return ""

    async def reconstruct_sources(self) -> Dict[str, Any]:
        """
        Rebuild the original source tree from loaded scripts' source maps
        (sourcesContent) plus an API endpoint inventory. External maps are
        fetched relative to their script URL with a size cap; inline data:
        maps are decoded in place.
        """
        from urllib.parse import urljoin

        import requests as _requests

        from agent_browser.source_reconstruct import decode_data_url, parse_sourcemap, reconstruct_tree

        maps = []
        errors = []
        for s_id, s_info in list(self.scripts.items()):
            sm_url = s_info.get("sourceMapURL") or ""
            if not sm_url:
                continue
            map_obj = None
            if sm_url.startswith("data:"):
                map_obj = decode_data_url(sm_url)
            else:
                try:
                    target = urljoin(s_info.get("url") or "", sm_url)
                    if not target.startswith(("http://", "https://")):
                        continue
                    resp = await asyncio.to_thread(_requests.get, target, timeout=5)
                    if len(resp.content) <= 10 * 1024 * 1024:
                        map_obj = resp.json()
                except Exception as exc:
                    errors.append({"scriptId": s_id, "error": str(exc)[:120]})
                    continue
            if map_obj:
                maps.append(parse_sourcemap(map_obj, s_info.get("url") or ""))
        tree = reconstruct_tree(maps)
        tree["maps"] = len(maps)
        tree["errors"] = errors
        return tree

    async def get_all_sources(self, include_content: bool = True) -> Dict[str, Any]:
        """
        Dumps complete sources bundle for the active page:
        - Full page HTML source (DOM)
        - All inline scripts and external JS scripts (with source code)
        - Sourcemap references and statistics
        """
        sid = self.get_session_id()
        meta = {}
        try:
            res = await self.cdp.send("Runtime.evaluate", {
                "expression": "JSON.stringify({ url: window.location.href, title: document.title })\n//# sourceURL=__agent_internal_meta__.js",
                "returnByValue": True
            }, session_id=sid)
            meta = json.loads(res.get("result", {}).get("value", "{}"))
        except Exception:
            pass

        html = await self.get_page_html()
        scripts_list = []
        sourcemaps_list = []

        for s_id, s_info in list(self.scripts.items()):
            content = None
            if include_content:
                content = await self.get_script_source(s_id)

            item = {
                "scriptId": s_id,
                "url": s_info.get("url", ""),
                "is_inline": s_info.get("is_inline", False),
                "lines": s_info.get("lines", 0),
                "length": s_info.get("length", 0),
                "sourceMapURL": s_info.get("sourceMapURL", "")
            }
            if include_content:
                item["content"] = content
            scripts_list.append(item)

            sm_url = s_info.get("sourceMapURL")
            if sm_url:
                sourcemaps_list.append({
                    "scriptId": s_id,
                    "script_url": s_info.get("url", ""),
                    "sourceMapURL": sm_url
                })

        return {
            "url": meta.get("url", ""),
            "title": meta.get("title", ""),
            "html": html,
            "html_length": len(html),
            "scripts_count": len(scripts_list),
            "scripts": scripts_list,
            "sourcemaps_count": len(sourcemaps_list),
            "sourcemaps": sourcemaps_list
        }

    async def dump_sources_to_disk(self, output_dir: str) -> Dict[str, Any]:
        """
        Saves all page sources (HTML, all JS scripts, sourcemaps, manifest) to disk.
        """
        os.makedirs(output_dir, exist_ok=True)
        bundle = await self.get_all_sources(include_content=True)

        # Write index.html
        html_file = os.path.join(output_dir, "index.html")
        with open(html_file, "w", encoding="utf-8", errors="replace") as f:
            f.write(bundle["html"])

        # Write scripts
        scripts_dir = os.path.join(output_dir, "scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        saved_scripts = []

        for s in bundle["scripts"]:
            s_id = s["scriptId"]
            content = s.get("content") or ""
            s_url = s.get("url", "")
            if s["is_inline"] or not s_url or s_url.startswith("[inline"):
                fname = f"inline_{s_id}.js"
            else:
                base_name = s_url.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
                clean = re.sub(r'[^\w\-.]', '_', base_name)
                if not clean.endswith(".js"):
                    clean += ".js"
                fname = f"{s_id}_{clean}"

            target_path = os.path.join(scripts_dir, fname)
            with open(target_path, "w", encoding="utf-8", errors="replace") as f:
                f.write(content)
            saved_scripts.append({
                "scriptId": s_id,
                "url": s_url,
                "file": fname,
                "size_bytes": len(content.encode("utf-8"))
            })

        # Write sourcemaps
        sourcemaps_dir = os.path.join(output_dir, "sourcemaps")
        saved_sourcemaps = []
        for sm in bundle.get("sourcemaps", []):
            sm_url = sm.get("sourceMapURL", "")
            s_url = sm.get("script_url", "")
            s_id = sm.get("scriptId", "sm")
            sm_content = None
            sm_fname = None

            if sm_url.startswith("data:application/json;base64,"):
                try:
                    b64_data = sm_url.split(",", 1)[1]
                    sm_content = base64.b64decode(b64_data).decode("utf-8", errors="replace")
                    sm_fname = f"{s_id}.js.map"
                except Exception:
                    pass
            elif sm_url.startswith("data:"):
                try:
                    import urllib.parse
                    raw_data = sm_url.split(",", 1)[1]
                    sm_content = urllib.parse.unquote(raw_data)
                    sm_fname = f"{s_id}.js.map"
                except Exception:
                    pass
            else:
                try:
                    import urllib.parse
                    import requests
                    full_sm_url = urllib.parse.urljoin(s_url, sm_url) if s_url else sm_url
                    if full_sm_url.startswith("http://") or full_sm_url.startswith("https://"):
                        r = requests.get(full_sm_url, timeout=3.0)
                        if r.status_code == 200:
                            sm_content = r.text
                            base_name = full_sm_url.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
                            clean = re.sub(r'[^\w\-.]', '_', base_name)
                            if not clean.endswith(".map"):
                                clean += ".map"
                            sm_fname = f"{s_id}_{clean}"
                except Exception:
                    pass

            if sm_content and sm_fname:
                os.makedirs(sourcemaps_dir, exist_ok=True)
                sm_path = os.path.join(sourcemaps_dir, sm_fname)
                with open(sm_path, "w", encoding="utf-8", errors="replace") as f:
                    f.write(sm_content)
                saved_sourcemaps.append({
                    "scriptId": s_id,
                    "sourceMapURL": sm_url,
                    "file": os.path.join("sourcemaps", sm_fname),
                    "size_bytes": len(sm_content.encode("utf-8"))
                })

        # Write manifest
        manifest = {
            "url": bundle["url"],
            "title": bundle["title"],
            "html_file": "index.html",
            "html_size_bytes": len(bundle["html"].encode("utf-8")),
            "scripts_count": len(saved_scripts),
            "scripts": saved_scripts,
            "sourcemaps_found": len(bundle["sourcemaps"]),
            "sourcemaps": bundle["sourcemaps"],
            "sourcemaps_saved": saved_sourcemaps
        }
        manifest_file = os.path.join(output_dir, "sources_manifest.json")
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        return {
            "output_dir": os.path.abspath(output_dir),
            "manifest_file": manifest_file,
            "html_file": html_file,
            "scripts_saved": len(saved_scripts),
            "sourcemaps_found": len(bundle["sourcemaps"]),
            "sourcemaps_saved": len(saved_sourcemaps)
        }
