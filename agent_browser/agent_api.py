import os
import base64
import asyncio
import time
from typing import Dict, Any, List, Optional

from agent_browser.cdp import BrowserProcessManager, CDPConnection, wait_for_navigation
from agent_browser.dom import DOMEngine
from agent_browser.network import NetworkEngine, InterceptRule
from agent_browser.runtime import RuntimeEngine
from agent_browser.tracer import ActionTracer
from agent_browser.storage import StorageEngine
from agent_browser.diagnostics import DiagnosticEngine
from agent_browser.user_tracker import UserTracker
from agent_browser.focus import FocusEngine
from agent_browser.source_inspector import SourceInspector
from agent_browser.proxy_engine import LocalProxyServer
from agent_browser.quiescence import NetworkActivityMonitor


class AgentBrowserController:
    """
    Unified High-Level Controller for Autonomous Agent Browser Debugging Platform.
    """
    def __init__(
        self,
        port: int = 9223,
        headless: bool = False,
        profile: Optional[str] = None,
        profile_dir: Optional[str] = None,
        smart_filter: bool = True,
        enable_proxy: bool = False,
        proxy_port: int = 8888,
        upstream_proxy: Optional[str] = None,
        launch_profile: str = "full",
        transport: Optional[str] = None
    ):
        self.port = port
        self.headless = headless
        # 'websocket' (default, --remote-debugging-port) or 'pipe-windows'
        # (--remote-debugging-pipe over inherited handles; no TCP port).
        self.transport = (transport or os.environ.get("AGENTBROWSER_TRANSPORT") or "websocket").lower()
        self.smart_filter = smart_filter
        self.enable_proxy = enable_proxy
        self.proxy_port = proxy_port
        self.upstream_proxy = upstream_proxy
        self.browser_mgr = BrowserProcessManager(
            port=port,
            headless=headless,
            profile=profile,
            profile_dir=profile_dir,
            launch_profile=launch_profile
        )
        self.profile_name = self.browser_mgr.profile_name
        self.profile_dir = self.browser_mgr.profile_dir
        self.cdp: Optional[CDPConnection] = None
        self.active_session_id: Optional[str] = None
        self.active_target_id: Optional[str] = None
        self.sessions: Dict[str, Dict[str, Any]] = {}  # sessionId -> targetInfo

        # Subsystems
        self.dom: Optional[DOMEngine] = None
        self.network: Optional[NetworkEngine] = None
        self.runtime: Optional[RuntimeEngine] = None
        self.tracer: Optional[ActionTracer] = None
        self.storage: Optional[StorageEngine] = None
        self.diagnostics: Optional[DiagnosticEngine] = None
        self.user_tracker: Optional[UserTracker] = None
        self.focus: Optional[FocusEngine] = None
        self.source_inspector: Optional[SourceInspector] = None
        self.perf = None
        self.proxy_server: Optional[LocalProxyServer] = None
        self.activity_monitor = NetworkActivityMonitor()
        self.journal = None
        # JavaScript dialogs block the renderer until handled; a policy must
        # always answer them. Default dismiss (safe); 'accept' is opt-in.
        self.dialog_policy = os.environ.get("AGENTBROWSER_DIALOG_POLICY", "dismiss").strip().lower()
        self.dialog_log: List[Dict[str, Any]] = []
        self.downloads: List[Dict[str, Any]] = []
        # Human handoff: while awaiting_human, agent-driven actions are
        # refused (a human has the browser); observation of status remains.
        self.awaiting_human = False
        self.handoff_log: List[Dict[str, Any]] = []
        self._handoff_started_at: Optional[float] = None
        self._handoff_action_baseline = 0
        self._handoff_reason = ""
        self._running = False
        self._reconnecting = False

    def get_active_session_id(self) -> Optional[str]:
        return self.active_session_id

    def list_tabs(self) -> List[Dict[str, Any]]:
        """Attached page targets that can be driven, with the active one marked."""
        return [
            {"session_id": sid, "target_id": t.get("targetId"), "url": t.get("url"),
             "title": t.get("title"), "active": sid == self.active_session_id}
            for sid, t in self.sessions.items() if t.get("type") == "page"
        ]

    def activate_tab(self, session_id: str) -> Dict[str, Any]:
        """
        Make an attached page target the active session. Engines resolve the
        active session per call, so subsequent view/click/navigate commands
        drive this tab. Raises ValueError for unknown or non-page sessions.
        """
        info = self.sessions.get(session_id)
        if not info:
            raise ValueError(f"unknown session: {session_id}")
        if info.get("type") != "page":
            raise ValueError(f"session {session_id} is a {info.get('type')}, not a page")
        self.active_session_id = session_id
        self.active_target_id = info.get("targetId")
        return {"active_session_id": self.active_session_id,
                "target_id": self.active_target_id, "url": info.get("url")}

    async def start(self, initial_url: str = "about:blank"):
        if self.enable_proxy:
            self.proxy_server = LocalProxyServer(port=self.proxy_port, upstream_proxy=self.upstream_proxy)
            self.proxy_server.start()

        if self.transport == "pipe-windows":
            import sys as _sys

            if _sys.platform != "win32":
                raise RuntimeError("transport 'pipe-windows' requires Windows; use launch_pipe_chrome on POSIX")
            from agent_browser.transport import spawn_chrome_pipe_windows

            extra: List[str] = []
            if self.browser_mgr.launch_profile == "light":
                extra.extend(BrowserProcessManager.LIGHT_FLAGS)
            elif self.browser_mgr.launch_profile == "hyper":
                extra.extend(BrowserProcessManager.HYPER_FLAGS)
            if self.headless:
                extra.extend(["--headless=new", "--disable-gpu"])
            proc, pipe_client = await spawn_chrome_pipe_windows(
                self.browser_mgr.browser_bin, self.browser_mgr.profile_dir,
                extra_flags=extra, url=initial_url)
            self.browser_mgr.proc = proc
            self.cdp = pipe_client
        else:
            ws_url = self.browser_mgr.launch(initial_url)
            self.cdp = CDPConnection(ws_url)
            await self.cdp.connect()
        self._running = True
        # Recover automatically if the debugger socket drops unexpectedly.
        self.cdp.on_drop = self._on_cdp_drop

        # Initialize engines
        self.dom = DOMEngine(self.cdp, self.get_active_session_id)
        self.network = NetworkEngine(self.cdp, self.get_active_session_id)
        self.network.smart_filter = self.smart_filter
        self.runtime = RuntimeEngine(self.cdp, self.get_active_session_id)
        self.tracer = ActionTracer(self.cdp, self.dom, self.network, self.runtime, self.get_active_session_id,
                                   activity_monitor=self.activity_monitor,
                                   strict_probe=os.environ.get("AGENTBROWSER_STRICT_SETTLE", "").strip().lower() in ("1", "true", "yes", "on"))
        self.storage = StorageEngine(self.cdp, self.get_active_session_id)
        self.diagnostics = DiagnosticEngine(self.dom, self.network, self.runtime, self.storage, self.tracer)
        self.user_tracker = UserTracker(self.cdp, self.get_active_session_id)
        self.focus = FocusEngine(self.cdp, self.get_active_session_id)
        self.source_inspector = SourceInspector(self.cdp, self.get_active_session_id)
        from agent_browser.perf import PerfEngine

        self.perf = PerfEngine(self.cdp, self.get_active_session_id)

        # Wire event handlers. The activity monitor is a synchronous
        # subscriber: its ledger cannot be shed by the async handler cap.
        self.cdp.add_sync_handler(self.activity_monitor.on_event)
        # Opt-in flight recorder (AGENTBROWSER_JOURNAL=<path>): sync, so the
        # journal cannot lose events to the async handler cap.
        from agent_browser.journal import JournalRecorder, journal_path_from_env

        journal_path = journal_path_from_env()
        self.journal = JournalRecorder(journal_path) if journal_path else None
        if self.journal is not None:
            self.cdp.add_sync_handler(self.journal.on_event)
        self.cdp.add_event_handler(self._on_cdp_event)

        # Auto-attach to all targets (pages, workers, iframes)
        await self.cdp.send("Target.setAutoAttach", {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True
        })

        # Downloads land in a confined directory and report progress events.
        try:
            download_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent_output", "downloads")
            os.makedirs(download_dir, exist_ok=True)
            await self.cdp.send("Browser.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": download_dir, "eventsEnabled": True,
            })
        except Exception:
            pass

        # Wait briefly for session attachment
        for _ in range(30):
            if self.active_session_id:
                break
            await asyncio.sleep(0.1)

        if not self.active_session_id:
            # Create a target explicitly if none auto-attached
            res = await self.cdp.send("Target.createTarget", {"url": initial_url})
            target_id = res.get("targetId")
            att = await self.cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
            self.active_session_id = att.get("sessionId")
            self.active_target_id = target_id
            self.sessions[self.active_session_id] = {"type": "page", "targetId": target_id}

        await self._enable_page_session(self.active_session_id)

    async def _enable_page_session(self, sid: Optional[str]):
        """Enable the domains a newly attached page needs before the first command."""
        if not sid or not self.cdp:
            return
        await self.cdp.send("Page.enable", {}, session_id=sid)
        try:
            # lifecycleEvent (load/networkIdle by loaderId) is never emitted
            # unless explicitly enabled; the quiescence/nav layers need it.
            await self.cdp.send("Page.setLifecycleEventsEnabled", {"enabled": True}, session_id=sid)
        except Exception:
            pass
        await self.cdp.send("DOM.enable", {}, session_id=sid)
        try:
            # Recursive auto-attach: cross-origin iframes (OOPIFs) and workers
            # get their own flattened sessions instead of staying invisible.
            await self.cdp.send("Target.setAutoAttach", {
                "autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True,
            }, session_id=sid)
        except Exception:
            pass
        # Domain enables are independent CDP commands; running them
        # concurrently cuts several sequential round trips from cold start.
        # return_exceptions keeps one failing domain from blocking the rest.
        await asyncio.gather(
            self.network.enable_network(session_id=sid),
            self.runtime.enable(session_id=sid),
            self.user_tracker.enable(session_id=sid),
            self.source_inspector.enable(session_id=sid),
            return_exceptions=True,
        )

    async def _enable_child_session(self, sid: Optional[str]):
        """
        Enable capture on a child target (iframe/worker/service worker).

        Without this, OOPIF and worker traffic, scripts, and exceptions never
        reach the engines, which key their state by request/script id and
        therefore work unchanged once the events actually flow.
        """
        if not sid or not self.cdp:
            return
        await asyncio.gather(
            self.network.enable_network(session_id=sid),
            self.runtime.enable(session_id=sid),
            self.source_inspector.enable_for_session(sid),
            return_exceptions=True,
        )

    async def reconnect(self) -> Dict[str, Any]:
        """
        Reconnect the debugger socket after a drop.

        connect() bumps the generation, so futures issued on the old socket
        stay failed. Commands issued after this method use the new connection.
        """
        if not self.cdp:
            raise RuntimeError("No CDP client to reconnect.")
        old_generation = self.cdp.generation
        try:
            await self.cdp.disconnect()
        except Exception:
            pass
        ws_url = self.browser_mgr.get_ws_debugger_url()
        self.cdp.ws_url = ws_url
        self.active_session_id = None
        self.active_target_id = None
        self.sessions.clear()
        await self.cdp.connect()
        self._running = True
        await self.cdp.send("Target.setAutoAttach", {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True
        })
        for _ in range(30):
            if self.active_session_id:
                break
            await asyncio.sleep(0.1)
        if self.active_session_id:
            await self._enable_page_session(self.active_session_id)
        return {
            "reconnected": True,
            "previous_generation": old_generation,
            "generation": self.cdp.generation,
            "sessionId": self.active_session_id,
        }

    def _on_cdp_drop(self, generation: int):
        """Schedule an automatic reconnect after an unexpected socket drop."""
        if not self._running:
            return
        if self.transport != "websocket":
            # Pipe transport: the browser exits when the pipe closes, so
            # there is nothing to reconnect to. The session is over.
            self._running = False
            return
        try:
            asyncio.get_running_loop().create_task(self._auto_reconnect(generation))
        except RuntimeError:
            pass

    async def _auto_reconnect(self, dropped_generation: int, max_attempts: int = 5):
        """
        Re-dial the debugger with exponential backoff. The generation counter in
        CDPConnection keeps stale futures failed, so a late reply on the old
        socket cannot complete a new command. Gives up if the browser is gone.
        """
        if self._reconnecting:
            return
        self._reconnecting = True
        delay = 0.5
        try:
            for _ in range(max_attempts):
                if not self._running:
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)
                try:
                    if not self.browser_mgr.is_port_ready():
                        continue
                    await self.reconnect()
                    self.cdp.on_drop = self._on_cdp_drop
                    return
                except Exception:
                    continue
        finally:
            self._reconnecting = False

    async def _on_cdp_event(self, data: Dict[str, Any]):
        method = data.get("method", "")
        params = data.get("params", {})

        if method == "Page.javascriptDialogOpening":
            # Always answer: an unhandled dialog freezes the page (and any
            # evaluate would hang behind it). Policy: dismiss unless the
            # operator opted into accept.
            accept = self.dialog_policy == "accept"
            self.dialog_log.append({
                "type": params.get("type"), "message": params.get("message"),
                "url": params.get("url"), "accepted": accept,
                "session_id": data.get("sessionId"),
            })
            if len(self.dialog_log) > 100:
                self.dialog_log.pop(0)
            try:
                await self.cdp.send("Page.handleJavaScriptDialog", {"accept": accept},
                                    session_id=data.get("sessionId") or self.active_session_id)
            except Exception:
                pass

        elif method in ("Browser.downloadWillBegin", "Page.downloadWillBegin"):
            self.downloads.append({
                "guid": params.get("guid"), "url": params.get("url"),
                "suggested_filename": params.get("suggestedFilename"), "state": "started",
            })

        elif method in ("Browser.downloadProgress", "Page.downloadProgress"):
            guid = params.get("guid")
            for dl in reversed(self.downloads):
                if dl.get("guid") == guid:
                    dl["state"] = params.get("state") or dl["state"]
                    dl["received_bytes"] = params.get("receivedBytes")
                    dl["total_bytes"] = params.get("totalBytes")
                    break

        if method == "Target.attachedToTarget":
            sid = params.get("sessionId")
            t_info = params.get("targetInfo", {})
            self.sessions[sid] = t_info
            if t_info.get("type") == "page":
                # Every page tab gets the FULL session setup (Page/DOM/
                # lifecycle/network/runtime/tracker/hooks), whether or not
                # it is the active tab -- otherwise a tab promoted after the
                # active one closes would lack the domains navigation and
                # inspection depend on.
                if not self.active_session_id:
                    self.active_session_id = sid
                    self.active_target_id = t_info.get("targetId")
                asyncio.create_task(self._enable_page_session(sid))
            elif t_info.get("type") in ("iframe", "worker", "service_worker"):
                asyncio.create_task(self._enable_child_session(sid))

        elif method == "Page.frameNavigated":
            # Keep tab listings honest: targetInfo.url is attach-time only.
            frame = params.get("frame") or {}
            sid = data.get("sessionId")
            if sid in self.sessions and not frame.get("parentId"):
                self.sessions[sid]["url"] = frame.get("url")

        elif method == "Target.detachedFromTarget":
            sid = params.get("sessionId")
            self.sessions.pop(sid, None)
            if self.active_session_id == sid:
                self.active_session_id = next((s for s, t in self.sessions.items() if t.get("type") == "page"), None)
                self.active_target_id = self.sessions.get(self.active_session_id, {}).get("targetId") if self.active_session_id else None

        # Forward to sub-engines
        if self.network:
            await self.network.handle_cdp_event(data)
        if self.runtime:
            await self.runtime.handle_cdp_event(data)
        if self.user_tracker:
            await self.user_tracker.handle_cdp_event(data)
        if self.source_inspector:
            await self.source_inspector.handle_cdp_event(data)

    def _require_agent_control(self, operation: str) -> None:
        if self.awaiting_human:
            raise RuntimeError(f"{operation} refused: awaiting human handoff (complete the handoff first)")

    def handoff_status(self) -> Dict[str, Any]:
        return {
            "awaiting_human": self.awaiting_human,
            "reason": self._handoff_reason,
            "started_at": self._handoff_started_at,
            "log": self.handoff_log[-10:],
        }

    async def _handoff_banner(self, show: bool, reason: str = "") -> None:
        if not self.runtime:
            return
        if show:
            expr = (
                "(() => { let b = document.getElementById('__agent_handoff_banner');"
                " if (!b) { b = document.createElement('div'); b.id = '__agent_handoff_banner';"
                " b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;"
                "background:#7c2d12;color:#fff;padding:8px 12px;font:14px sans-serif;text-align:center';"
                " document.documentElement.appendChild(b); }"
                f" b.textContent = 'Agent paused -- human in control. {reason}'; return true; }})()"
            )
        else:
            expr = "(() => { const b = document.getElementById('__agent_handoff_banner'); if (b) b.remove(); return true; })()"
        try:
            await self.runtime.evaluate(expr)
        except Exception:
            pass

    async def start_handoff(self, reason: str = "", instructions: str = "") -> Dict[str, Any]:
        """Pause the agent and hand the browser to a human."""
        if self.awaiting_human:
            return self.handoff_status()
        self.awaiting_human = True
        self._handoff_started_at = time.time()
        self._handoff_reason = reason or ""
        self._handoff_action_baseline = len(self.user_tracker.action_history) if self.user_tracker else 0
        self.handoff_log.append({
            "event": "handoff_started", "reason": reason, "instructions": instructions,
            "timestamp": self._handoff_started_at,
        })
        await self._handoff_banner(True, reason)
        return self.handoff_status()

    async def complete_handoff(self, summary: str = "") -> Dict[str, Any]:
        """Resume agent control; summarize what the human did meanwhile."""
        if not self.awaiting_human:
            return self.handoff_status()
        actions: List[Dict[str, Any]] = []
        if self.user_tracker:
            history = self.user_tracker.get_actions(limit=100000)
            actions = history[self._handoff_action_baseline:] if len(history) >= self._handoff_action_baseline else history
        entry = {
            "event": "handoff_complete", "summary": summary,
            "human_actions_count": len(actions), "human_actions": actions[-50:],
            "timestamp": time.time(),
        }
        self.handoff_log.append(entry)
        self.awaiting_human = False
        self._handoff_reason = ""
        await self._handoff_banner(False)
        return {**self.handoff_status(), "completion": entry}

    async def navigate(self, url: str) -> Dict[str, Any]:
        self._require_agent_control("navigate")
        sid = self.active_session_id
        result = await wait_for_navigation(self.cdp, sid, url)
        if result.get("navigated") and self.source_inspector:
            await self.source_inspector._enable_debugger(sid)
        if result.get("navigated") and self.user_tracker:
            try:
                await self.user_tracker.inject_tracker()
            except Exception:
                pass
        return result

    async def get_agent_view(self) -> str:
        """
        Default agent view: the budgeted accessibility-tree action graph
        (node-identity refs, no page pollution). Falls back to the legacy
        injected-JS view when the AX tree is unavailable or empty, or when
        AGENTBROWSER_VIEW=legacy pins the old representation.
        """
        if os.environ.get("AGENTBROWSER_VIEW", "").strip().lower() != "legacy":
            try:
                view = await self.dom.format_axtree_view()
                if self.dom._ax_refs:
                    return view
            except Exception:
                pass
        return await self.dom.format_agent_view()

    async def get_interactive_snapshot(self) -> Dict[str, Any]:
        return await self.dom.get_interactive_snapshot()

    async def trace_action(self, action_type: str, target: Optional[str] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._require_agent_control("trace_action")
        return await self.tracer.trace_action(action_type, target=target, params=params)

    async def evaluate(self, expression: str) -> Dict[str, Any]:
        self._require_agent_control("evaluate")
        return await self.runtime.evaluate(expression)

    async def take_screenshot(self, output_path: str = "screenshot.png", full_page: bool = False) -> str:
        # No screenshots while a human is in control (privacy).
        self._require_agent_control("screenshot")
        sid = self.active_session_id
        params = {"format": "png"}
        if full_page:
            params["captureBeyondViewport"] = True
        res = await self.cdp.send("Page.captureScreenshot", params, session_id=sid)
        data_b64 = res.get("data")
        raw = base64.b64decode(data_b64)
        abs_path = os.path.abspath(output_path)
        with open(abs_path, "wb") as f:
            f.write(raw)
        return abs_path

    async def get_page_html(self) -> str:
        if not self.source_inspector:
            return ""
        return await self.source_inspector.get_page_html()

    async def get_all_sources(self, include_content: bool = True) -> Dict[str, Any]:
        if not self.source_inspector:
            return {}
        return await self.source_inspector.get_all_sources(include_content=include_content)

    async def dump_sources_to_disk(self, output_dir: str) -> Dict[str, Any]:
        if not self.source_inspector:
            return {}
        return await self.source_inspector.dump_sources_to_disk(output_dir)

    async def stop(self):
        self._running = False
        if getattr(self, "journal", None) is not None:
            try:
                self.journal.close()
            except Exception:
                pass
            self.journal = None
        if self.proxy_server:
            self.proxy_server.stop()
        if self.cdp:
            try:
                await self.cdp.send("Browser.close", {}, timeout=2.0)
            except Exception:
                pass
            await self.cdp.disconnect()
        self.browser_mgr.stop()
