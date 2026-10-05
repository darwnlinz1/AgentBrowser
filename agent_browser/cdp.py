import os
import sys
import time
import json
import asyncio
import subprocess
import requests
import websockets
from typing import Dict, Any, Optional, Callable, List

STANDARD_BROWSER_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
]

def find_system_browser(custom_path: Optional[str] = None) -> Optional[str]:
    if custom_path and os.path.exists(custom_path):
        return custom_path

    try:
        import winreg
        reg_keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
        ]
        for hive, subkey in reg_keys:
            try:
                with winreg.OpenKey(hive, subkey) as k:
                    val, _ = winreg.QueryValueEx(k, "")
                    if val and os.path.exists(val):
                        return val
            except Exception:
                pass
    except Exception:
        pass

    for p in STANDARD_BROWSER_PATHS:
        if os.path.exists(p):
            return p
    return None


class CDPConnection:
    """
    Multiplexed Asynchronous Chrome DevTools Protocol Client.
    Supports browser-level commands, target auto-attachment, and session routing.
    """
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws = None
        self.cmd_id = 0
        self.pending_futures: Dict[int, asyncio.Future] = {}
        self.event_handlers: List[Callable[[Dict[str, Any]], Any]] = []
        self.running = False
        self._listener_task: Optional[asyncio.Task] = None

    async def connect(self):
        self.ws = await websockets.connect(
            self.ws_url,
            max_size=100_000_000,
            ping_interval=20,
            ping_timeout=20
        )
        self.running = True
        self._listener_task = asyncio.create_task(self._listen_loop())

    async def disconnect(self):
        self.running = False
        if self._listener_task:
            self._listener_task.cancel()
        if self.ws:
            await self.ws.close()

    def add_event_handler(self, handler: Callable[[Dict[str, Any]], Any]):
        self.event_handlers.append(handler)

    def remove_event_handler(self, handler: Callable[[Dict[str, Any]], Any]):
        if handler in self.event_handlers:
            self.event_handlers.remove(handler)

    async def send(self, method: str, params: Optional[Dict[str, Any]] = None, session_id: Optional[str] = None, timeout: float = 15.0) -> Dict[str, Any]:
        if not self.ws or not self.running:
            raise RuntimeError("CDP connection is not established or already closed.")

        self.cmd_id += 1
        cid = self.cmd_id
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.pending_futures[cid] = fut

        msg: Dict[str, Any] = {
            "id": cid,
            "method": method,
            "params": params or {}
        }
        if session_id:
            msg["sessionId"] = session_id

        await self.ws.send(json.dumps(msg))

        try:
            res = await asyncio.wait_for(fut, timeout=timeout)
            if "error" in res:
                raise RuntimeError(f"CDP Error [{method}]: {res['error'].get('message', res['error'])}")
            return res.get("result", {})
        finally:
            self.pending_futures.pop(cid, None)

    async def _listen_loop(self):
        while self.running:
            try:
                raw = await self.ws.recv()
                data = json.loads(raw)
            except websockets.exceptions.ConnectionClosed:
                break
            except asyncio.CancelledError:
                break
            except Exception:
                continue

            msg_id = data.get("id")
            if msg_id and msg_id in self.pending_futures:
                fut = self.pending_futures.get(msg_id)
                if fut and not fut.done():
                    fut.set_result(data)
                continue

            # Asynchronously dispatch to event handlers
            for handler in self.event_handlers:
                try:
                    res = handler(data)
                    if asyncio.iscoroutine(res):
                        asyncio.create_task(res)
                except Exception:
                    pass


class BrowserProcessManager:
    """
    Manages browser process lifecycle (launch, port binding, profile isolation, termination).
    """
    def __init__(self, port: int = 9223, headless: bool = False, profile_dir: Optional[str] = None, browser_bin: Optional[str] = None):
        self.port = port
        self.headless = headless
        self.browser_bin = browser_bin or find_system_browser()
        if not self.browser_bin:
            raise FileNotFoundError("Could not find Chrome or Edge executable on this machine.")

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.profile_dir = profile_dir or os.path.join(base_dir, "debug_browser_profile")
        self.proc: Optional[subprocess.Popen] = None
        self.attached_existing = False

    def is_port_ready(self) -> bool:
        try:
            r = requests.get(f"http://127.0.0.1:{self.port}/json/version", timeout=1.0)
            return r.status_code == 200 and "webSocketDebuggerUrl" in r.json()
        except Exception:
            return False

    def get_ws_debugger_url(self) -> str:
        r = requests.get(f"http://127.0.0.1:{self.port}/json/version", timeout=3.0)
        r.raise_for_status()
        url = r.json().get("webSocketDebuggerUrl")
        if not url:
            raise RuntimeError("Target returned 200 but webSocketDebuggerUrl was empty.")
        return url

    def launch(self, url: str = "about:blank") -> str:
        if self.is_port_ready():
            self.attached_existing = True
            return self.get_ws_debugger_url()

        os.makedirs(self.profile_dir, exist_ok=True)
        cmd = [
            self.browser_bin,
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=OptimizationGuideModelDownloading,OptimizationHintsFetching",
        ]
        if self.headless:
            cmd.extend(["--headless=new", "--disable-gpu"])
        cmd.append(url)

        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        for _ in range(40):
            if self.is_port_ready():
                return self.get_ws_debugger_url()
            time.sleep(0.25)

        raise TimeoutError(f"Browser did not open DevTools endpoint on port {self.port} within timeout.")

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
