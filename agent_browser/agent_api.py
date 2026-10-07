import os
import base64
import asyncio
from typing import Dict, Any, List, Optional

from agent_browser.cdp import BrowserProcessManager, CDPConnection
from agent_browser.dom import DOMEngine
from agent_browser.network import NetworkEngine, InterceptRule
from agent_browser.runtime import RuntimeEngine
from agent_browser.tracer import ActionTracer
from agent_browser.storage import StorageEngine
from agent_browser.diagnostics import DiagnosticEngine
from agent_browser.user_tracker import UserTracker
from agent_browser.focus import FocusEngine


class AgentBrowserController:
    """
    Unified High-Level Controller for Autonomous Agent Browser Debugging Platform.
    """
    def __init__(self, port: int = 9223, headless: bool = False, profile_dir: Optional[str] = None):
        self.port = port
        self.headless = headless
        self.profile_dir = profile_dir
        self.browser_mgr = BrowserProcessManager(port=port, headless=headless, profile_dir=profile_dir)
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
        self._running = False

    def get_active_session_id(self) -> Optional[str]:
        return self.active_session_id

    async def start(self, initial_url: str = "about:blank"):
        ws_url = self.browser_mgr.launch(initial_url)
        self.cdp = CDPConnection(ws_url)
        await self.cdp.connect()
        self._running = True

        # Initialize engines
        self.dom = DOMEngine(self.cdp, self.get_active_session_id)
        self.network = NetworkEngine(self.cdp, self.get_active_session_id)
        self.runtime = RuntimeEngine(self.cdp, self.get_active_session_id)
        self.tracer = ActionTracer(self.cdp, self.dom, self.network, self.runtime, self.get_active_session_id)
        self.storage = StorageEngine(self.cdp, self.get_active_session_id)
        self.diagnostics = DiagnosticEngine(self.dom, self.network, self.runtime, self.storage, self.tracer)
        self.user_tracker = UserTracker(self.cdp, self.get_active_session_id)
        self.focus = FocusEngine(self.cdp, self.get_active_session_id)

        # Wire event handlers
        self.cdp.add_event_handler(self._on_cdp_event)

        # Auto-attach to all targets (pages, workers, iframes)
        await self.cdp.send("Target.setAutoAttach", {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True
        })

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

        # Enable core domains on active session
        sid = self.active_session_id
        await self.cdp.send("Page.enable", {}, session_id=sid)
        await self.cdp.send("DOM.enable", {}, session_id=sid)
        await self.network.enable_network()
        await self.runtime.enable()
        await self.user_tracker.enable()

    async def _on_cdp_event(self, data: Dict[str, Any]):
        method = data.get("method", "")
        params = data.get("params", {})

        if method == "Target.attachedToTarget":
            sid = params.get("sessionId")
            t_info = params.get("targetInfo", {})
            self.sessions[sid] = t_info
            if t_info.get("type") == "page" and not self.active_session_id:
                self.active_session_id = sid
                self.active_target_id = t_info.get("targetId")

        elif method == "Target.detachedFromTarget":
            sid = params.get("sessionId")
            self.sessions.pop(sid, None)
            if self.active_session_id == sid:
                self.active_session_id = next((s for s, t in self.sessions.items() if t.get("type") == "page"), None)

        # Forward to sub-engines
        if self.network:
            await self.network.handle_cdp_event(data)
        if self.runtime:
            await self.runtime.handle_cdp_event(data)
        if self.user_tracker:
            await self.user_tracker.handle_cdp_event(data)

    async def navigate(self, url: str) -> Dict[str, Any]:
        if not url.startswith("http://") and not url.startswith("https://") and not url.startswith("about:"):
            url = "https://" + url
        sid = self.active_session_id
        res = await self.cdp.send("Page.navigate", {"url": url}, session_id=sid)
        # Settle wait
        await asyncio.sleep(1.0)
        # Re-inject tracker if needed
        if self.user_tracker:
            await self.user_tracker.inject_tracker()
        return res

    async def get_agent_view(self) -> str:
        return await self.dom.format_agent_view()

    async def get_interactive_snapshot(self) -> Dict[str, Any]:
        return await self.dom.get_interactive_snapshot()

    async def trace_action(self, action_type: str, target: Optional[str] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self.tracer.trace_action(action_type, target=target, params=params)

    async def evaluate(self, expression: str) -> Dict[str, Any]:
        return await self.runtime.evaluate(expression)

    async def take_screenshot(self, output_path: str = "screenshot.png", full_page: bool = False) -> str:
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

    async def stop(self):
        self._running = False
        if self.cdp:
            await self.cdp.disconnect()
        self.browser_mgr.stop()
