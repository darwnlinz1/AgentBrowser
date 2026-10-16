import os
import sys
import time
import json
import asyncio
import subprocess
import requests
import websockets
import re
from typing import Dict, Any, Optional, Callable, List, Tuple

from agent_browser import codec as _codec
from agent_browser.evidence import is_within_dir
from agent_browser.ringbuffer import EventRingBuffer


def _connection_closed_types():
    """Match whichever ConnectionClosed this websockets build actually raises."""
    types = []
    for candidate in (
        getattr(websockets, "ConnectionClosed", None),
        getattr(getattr(websockets, "exceptions", None), "ConnectionClosed", None),
    ):
        if isinstance(candidate, type) and candidate not in types:
            types.append(candidate)
    return tuple(types) or (Exception,)


_CONNECTION_CLOSED = _connection_closed_types()

STANDARD_BROWSER_PATHS = [
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    # Linux
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge",
    "/usr/bin/brave-browser",
    "/snap/bin/chromium",
]

# Names resolved via PATH when no absolute candidate exists.
_BROWSER_PATH_NAMES = [
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "chrome", "microsoft-edge", "msedge", "brave-browser", "brave",
]

def find_system_browser(custom_path: Optional[str] = None) -> Optional[str]:
    if custom_path and os.path.exists(custom_path):
        return custom_path

    env_browser = os.environ.get("BROWSER") or os.environ.get("AGENTBROWSER_BROWSER")
    if env_browser and os.path.exists(env_browser):
        return env_browser

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
        if p and os.path.exists(p):
            return p

    import shutil

    for name in _BROWSER_PATH_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


class CDPConnection:
    """
    Multiplexed Asynchronous Chrome DevTools Protocol Client.
    Supports browser-level commands, target auto-attachment, and session routing.

    generation increments on every connect and on every close. A send captures
    the generation it was issued on. When the socket drops, that generation's
    futures fail immediately. A later reconnect uses a new generation, so an
    old future cannot be completed by a reply on the new socket.
    """
    def __init__(self, ws_url: str, event_buffer: Optional[EventRingBuffer] = None):
        self.ws_url = ws_url
        self.ws = None
        self.cmd_id = 0
        self.pending_futures: Dict[int, asyncio.Future] = {}
        self.event_handlers: List[Callable[[Dict[str, Any]], Any]] = []
        self.running = False
        self._listener_task: Optional[asyncio.Task] = None
        self.generation = 0
        self._max_handler_tasks = 64
        self._handler_tasks: set = set()
        # Optional Phase P staging buffer; when present, stats flow into
        # dropped_events accounting instead of ad-hoc shedding.
        self.event_buffer = event_buffer
        # Visibility + resilience: count shed events instead of dropping them
        # silently, and let an owner hook reconnect after an unexpected drop.
        self.dropped_events = 0
        self.on_drop: Optional[Callable[[int], Any]] = None
        # Synchronous subscribers run inline in the listener, before the
        # async fan-out. They are never subject to the handler-task cap, so
        # they are the only safe place for must-not-lose bookkeeping (a
        # dropped Fetch pause or activity event cannot corrupt their state).
        # They must be O(1) and never await.
        self.sync_handlers: List[Callable[[Dict[str, Any]], None]] = []
        self.sync_handler_errors = 0
        # Cycle 51: method-interest registry. A handler may declare the CDP
        # methods it consumes; the listener then skips the full params parse
        # for events nobody declared interest in (counted, never silent).
        # Handlers without a declaration stay catch-all.
        self.skipped_events = 0
        self._sync_interest: Dict[Any, Any] = {}
        self._event_interest: Dict[Any, Any] = {}
        # Cycle 52: per-session FIFO lanes. Async handlers for one session
        # run sequentially in arrival order (a loadingFinished can no longer
        # overtake its requestWillBeSent); lanes are independent, so one
        # stalled session cannot block another. Overflow spills to the old
        # capped task pool and is counted, never silent.
        self._lane_maxsize = 1024
        self._lanes: Dict[str, asyncio.Queue] = {}
        self._lane_workers: Dict[str, asyncio.Task] = {}
        self.lane_enqueued = 0
        self.lane_spilled = 0

    def _fail_pending(self, generation: int, reason: str):
        """Fail only futures issued on this generation. Leave a newer generation alone."""
        err = ConnectionError(reason)
        for cid, fut in list(self.pending_futures.items()):
            if getattr(fut, "_cdp_generation", None) != generation:
                continue
            self.pending_futures.pop(cid, None)
            if fut and not fut.done():
                fut.set_exception(err)

    async def connect(self):
        # A reconnect must not inherit the previous socket's command ids or futures.
        self.generation += 1
        self.cmd_id = 0
        self.ws = await websockets.connect(
            self.ws_url,
            max_size=100_000_000,
            ping_interval=20,
            ping_timeout=20,
            # permessage-deflate costs Chrome ~20us/frame to compress CDP
            # traffic nobody asked to compress; loopback does not need it.
            compression=None
        )
        self.running = True
        self._listener_task = asyncio.create_task(self._listen_loop())

    async def disconnect(self):
        generation = self.generation
        self.running = False
        self.generation += 1
        if self._listener_task:
            self._listener_task.cancel()
            self._listener_task = None
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        self._fail_pending(generation, "CDP connection closed")
        for task in list(self._handler_tasks):
            task.cancel()
        self._handler_tasks.clear()
        self._close_lanes()

    @staticmethod
    def _resolve_interest(handler, methods):
        if methods is not None:
            return frozenset(methods)
        declared = getattr(handler, "cdp_methods", None)
        return frozenset(declared) if declared else None

    @staticmethod
    def _interested(handlers, interest, method):
        return [h for h in handlers
                if interest.get(h) is None or method in interest[h]]

    def add_event_handler(self, handler: Callable[[Dict[str, Any]], Any], methods=None):
        self.event_handlers.append(handler)
        self._event_interest[handler] = self._resolve_interest(handler, methods)

    def remove_event_handler(self, handler: Callable[[Dict[str, Any]], Any]):
        if handler in self.event_handlers:
            self.event_handlers.remove(handler)
        self._event_interest.pop(handler, None)

    def add_sync_handler(self, handler: Callable[[Dict[str, Any]], None], methods=None):
        """Register an inline (never-dropped) synchronous event subscriber."""
        self.sync_handlers.append(handler)
        self._sync_interest[handler] = self._resolve_interest(handler, methods)

    def remove_sync_handler(self, handler: Callable[[Dict[str, Any]], None]):
        if handler in self.sync_handlers:
            self.sync_handlers.remove(handler)
        self._sync_interest.pop(handler, None)

    async def _lane_loop(self, key: str):
        queue = self._lanes[key]
        while True:
            coro = await queue.get()
            try:
                await coro
            except Exception:
                pass
            finally:
                queue.task_done()

    def _enqueue_lane(self, handler, data: Dict[str, Any], critical: bool = False):
        """Run one async handler call inside its session's FIFO lane."""
        try:
            coro = handler(data)
        except Exception:
            return
        if not asyncio.iscoroutine(coro):
            return
        key = data.get("sessionId") or ""
        queue = self._lanes.get(key)
        if queue is None:
            queue = asyncio.Queue(maxsize=self._lane_maxsize)
            self._lanes[key] = queue
            self._lane_workers[key] = asyncio.create_task(self._lane_loop(key))
        try:
            queue.put_nowait(coro)
            self.lane_enqueued += 1
        except asyncio.QueueFull:
            # Counted spill: ordering is only guaranteed while the lane
            # absorbs the flow; the overflow counter makes loss of that
            # guarantee visible instead of silent.
            self.lane_spilled += 1
            self._schedule_handler(coro, critical=critical)

    def _close_lanes(self):
        for task in list(self._lane_workers.values()):
            task.cancel()
        self._lane_workers.clear()
        for queue in list(self._lanes.values()):
            while not queue.empty():
                try:
                    coro = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if asyncio.iscoroutine(coro):
                    coro.close()
                    # Counted, never silent: these handler calls die with
                    # the socket that carried their events.
                    self.dropped_events += 1
                    if self.event_buffer is not None:
                        self.event_buffer.dropped += 1
        self._lanes.clear()

    def _schedule_handler(self, coro, critical: bool = False):
        # A Fetch.requestPaused handler must never be dropped: a dropped pause
        # leaves that request hanging until the Fetch timeout, which can freeze
        # the page under interception. Other events may be shed under extreme
        # load, but the drop is counted, never silent.
        if not critical and len(self._handler_tasks) >= self._max_handler_tasks:
            self.dropped_events += 1
            if self.event_buffer is not None:
                self.event_buffer.dropped += 1
            coro.close()
            return
        if self.event_buffer is not None:
            self.event_buffer.enqueued += 1
        task = asyncio.create_task(coro)
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    async def send(self, method: str, params: Optional[Dict[str, Any]] = None, session_id: Optional[str] = None, timeout: float = 15.0) -> Dict[str, Any]:
        if not self.ws or not self.running:
            raise ConnectionError("CDP connection is not established or already closed.")

        generation = self.generation
        self.cmd_id += 1
        cid = self.cmd_id
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        fut._cdp_generation = generation
        self.pending_futures[cid] = fut

        msg: Dict[str, Any] = {
            "id": cid,
            "method": method,
            "params": params or {}
        }
        if session_id:
            msg["sessionId"] = session_id

        try:
            await self.ws.send(_codec.dumps(msg))
        except Exception as exc:
            self.pending_futures.pop(cid, None)
            if not fut.done():
                fut.cancel()
            raise ConnectionError(f"CDP send failed: {exc}") from exc

        try:
            res = await asyncio.wait_for(fut, timeout=timeout)
            if isinstance(res, dict):
                if "error" in res:
                    raise RuntimeError(f"CDP Error [{method}]: {res['error'].get('message', res['error'])}")
                return res.get("result", {})
            # Raw-envelope reply: parse only the result slice, here on demand.
            # (error is an empty Raw when absent, so truthiness is the test.)
            if getattr(res, "error", None):
                err = _codec.decode_raw(res.error)
                message = err.get("message", err) if isinstance(err, dict) else err
                raise RuntimeError(f"CDP Error [{method}]: {message}")
            return _codec.decode_raw(getattr(res, "result", None)) or {}
        except asyncio.TimeoutError:
            self.pending_futures.pop(cid, None)
            raise
        finally:
            # A close may already have removed it. Do not resurrect a finished future.
            current = self.pending_futures.get(cid)
            if current is fut:
                self.pending_futures.pop(cid, None)

    async def _listen_loop(self, generation: Optional[int] = None):
        if generation is None:
            generation = self.generation
        while self.running and self.generation == generation:
            envelope = None
            try:
                raw = await self.ws.recv()
                # Fast path: route command replies by envelope id without
                # parsing params/result. Events still get a full parse below.
                envelope = _codec.decode_envelope(raw)
                if envelope is not None and envelope.id is not None:
                    if self.generation != generation:
                        break
                    fut = self.pending_futures.get(envelope.id)
                    if fut and not fut.done() and getattr(fut, "_cdp_generation", None) == generation:
                        fut.set_result(envelope)
                    continue
                sync_interested = event_interested = None
                if envelope is not None and envelope.method:
                    # Cheap routing decision: if no registered handler
                    # declared interest in this method, skip the full
                    # params parse entirely (counted as skipped_events).
                    sync_interested = self._interested(
                        self.sync_handlers, self._sync_interest, envelope.method)
                    event_interested = self._interested(
                        self.event_handlers, self._event_interest, envelope.method)
                    if not sync_interested and not event_interested:
                        self.skipped_events += 1
                        continue
                data = _codec.loads(raw)
            except asyncio.CancelledError:
                break
            except _CONNECTION_CLOSED:
                self.running = False
                self._fail_pending(generation, "CDP socket closed")
                # Lanes belong to the dead socket: queued handler calls
                # must not execute against a future reconnected session.
                self._close_lanes()
                cb = self.on_drop
                if cb:
                    try:
                        cb(generation)
                    except Exception:
                        pass
                break
            except Exception:
                if not self.running or self.generation != generation:
                    break
                continue

            # A reply that arrives after reconnect belongs to a dead generation.
            if self.generation != generation:
                break

            msg_id = data.get("id")
            if msg_id and msg_id in self.pending_futures:
                fut = self.pending_futures.get(msg_id)
                if fut and not fut.done() and getattr(fut, "_cdp_generation", None) == generation:
                    fut.set_result(data)
                continue

            if data.get("method"):
                for sync_fn in list(sync_interested if sync_interested is not None
                                     else self.sync_handlers):
                    try:
                        sync_fn(data)
                    except Exception:
                        self.sync_handler_errors += 1

            critical = data.get("method") == "Fetch.requestPaused"
            for handler in list(event_interested if event_interested is not None
                                else self.event_handlers):
                self._enqueue_lane(handler, data, critical=critical)


def normalize_navigation_url(url: str) -> str:
    if not url:
        return url
    if url.startswith(("http://", "https://", "about:", "file:", "data:")):
        return url
    return "https://" + url


async def wait_for_navigation(cdp, session_id: Optional[str], url: str, timeout: float = 8.0) -> Dict[str, Any]:
    """
    Start Page.navigate and wait for the load event of that navigation.

    CDP can return a successful result that still contains errorText
    (for example net::ERR_UNSAFE_PORT). That is a failed navigation.
    """
    target = normalize_navigation_url(url)
    loop = asyncio.get_running_loop()
    loaded = loop.create_future()
    seen_loader: Dict[str, Any] = {}
    lifecycle_loads: List[Dict[str, Any]] = []
    same_document = loop.create_future()
    expected_loader: Dict[str, Any] = {"loaderId": None}
    reply_seen: Dict[str, bool] = {"seen": False}

    def _on_event(data: Dict[str, Any]):
        method = data.get("method")
        event_sid = data.get("sessionId")
        if session_id and event_sid and event_sid != session_id:
            return
        params = data.get("params") or {}
        if method == "Page.lifecycleEvent" and params.get("name") == "load":
            # loaderId-filtered load signal. A lifecycle load only resolves
            # the wait when its loaderId matches THIS navigation; a stale or
            # iframe load is buffered for reporting but never resolves.
            # (The expected loaderId is known only after the navigate reply,
            # so pre-reply events are buffered and re-scanned below.)
            lifecycle_loads.append(params)
            if expected_loader["loaderId"] and params.get("loaderId") == expected_loader["loaderId"]:
                if not loaded.done():
                    loaded.set_result({"lifecycle": params})
            return
        if method == "Page.navigatedWithinDocument":
            if not same_document.done():
                same_document.set_result(params)
            return
        if method != "Page.loadEventFired":
            return
        # loadEventFired carries no loaderId/frameId, so a load that fires
        # before the navigate reply may belong to the previous document.
        # Only a post-reply load can be attributed to this navigation.
        if not reply_seen["seen"]:
            return
        if not loaded.done():
            loaded.set_result(params)

    cdp.add_event_handler(_on_event)
    try:
        res = await cdp.send("Page.navigate", {"url": target}, session_id=session_id, timeout=timeout)
    except Exception as exc:
        cdp.remove_event_handler(_on_event)
        return {
            "navigated": False,
            "url": target,
            "error": str(exc),
            "loaded": False,
            "result": None,
        }

    reply_seen["seen"] = True
    error_text = res.get("errorText")
    loader_id = res.get("loaderId")
    if error_text:
        cdp.remove_event_handler(_on_event)
        if not loaded.done():
            loaded.cancel()
        if not same_document.done():
            same_document.cancel()
        return {
            "navigated": False,
            "url": target,
            "error": error_text,
            "loaded": False,
            "loaderId": loader_id,
            "result": res,
        }

    # Same-document navigation (hash/history): no loaderId and no load event
    # will ever fire. Resolve on navigatedWithinDocument (or a short grace
    # window) instead of burning the full timeout.
    if not loader_id:
        try:
            sd = await asyncio.wait_for(same_document, timeout=min(0.5, timeout))
            cdp.remove_event_handler(_on_event)
            return {
                "navigated": True, "url": target, "error": None, "loaded": True,
                "same_document": True, "loaderId": None, "loadEvent": sd, "result": res,
            }
        except asyncio.TimeoutError:
            pass

    # Now that the navigate reply is known, arm the loaderId filter and
    # re-scan lifecycle loads buffered before the reply arrived.
    expected_loader["loaderId"] = loader_id
    buffered_match = next(
        (p for p in lifecycle_loads if loader_id and p.get("loaderId") == loader_id), None)
    if buffered_match is not None and not loaded.done():
        loaded.set_result({"lifecycle": buffered_match})

    load_timed_out = False
    try:
        seen_loader = await asyncio.wait_for(loaded, timeout=timeout)
    except asyncio.TimeoutError:
        seen_loader = {}
        load_timed_out = True
    finally:
        cdp.remove_event_handler(_on_event)

    lifecycle_match = next(
        (p for p in lifecycle_loads if loader_id and p.get("loaderId") == loader_id), None)

    # Chrome accepted the navigation (no errorText). The load event is a
    # separate fact: a timeout means we did not observe it, not that it fired.
    return {
        "navigated": True,
        "url": target,
        "error": None,
        "loaded": not load_timed_out,
        "loaderId": loader_id,
        "lifecycleLoaderId": (lifecycle_match or {}).get("loaderId"),
        "loadEvent": seen_loader,
        "result": res,
    }


def profiles_root() -> str:
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.abspath(os.path.join(base_dir, "profiles"))


def _sanitize_profile_segment(profile: str) -> str:
    """
    A relative profile name is one path segment under profiles/.
    Separators, dots, and sibling-prefix tricks are stripped so the result
    cannot resolve to a sibling of the profiles root.
    """
    raw = str(profile or "").strip()
    raw = raw.replace("/", "_").replace("\\", "_")
    clean_name = re.sub(r"[^\w\-]", "_", raw).strip("._ ")
    if not clean_name or clean_name in ("..", ".", "_"):
        return "custom"
    return clean_name


def resolve_profile_dir(profile: Optional[str] = None, profile_dir: Optional[str] = None) -> Tuple[str, str]:
    """
    Resolves profile name and absolute profile directory path.
    - If profile_dir is given: uses that absolute path. The caller chose it.
    - If profile is an absolute path: uses that path. The caller chose it.
    - A relative profile name, including one with separators, is a single
      sanitized segment under <workspace>/profiles/. It cannot escape, and it
      cannot land on a sibling of that root.
    - If neither given: defaults to <workspace>/debug_browser_profile.
    Returns (profile_name, profile_dir).
    """
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root = profiles_root()

    if profile_dir:
        abs_path = os.path.abspath(profile_dir)
        name = os.path.basename(abs_path.rstrip("\\/")) or "custom"
        return name, abs_path

    if profile:
        profile_str = str(profile).strip()
        if os.path.isabs(profile_str):
            abs_path = os.path.abspath(profile_str)
            name = os.path.basename(abs_path.rstrip("\\/")) or "custom"
            return name, abs_path

        clean_name = _sanitize_profile_segment(profile_str)
        if clean_name == "default":
            return "default", os.path.join(base_dir, "debug_browser_profile")

        target_dir = os.path.abspath(os.path.join(root, clean_name))
        if not is_within_dir(target_dir, root) or os.path.dirname(target_dir) != root:
            clean_name = "custom"
            target_dir = os.path.join(root, clean_name)
        return clean_name, target_dir

    return "default", os.path.join(base_dir, "debug_browser_profile")


def list_profiles() -> List[Dict[str, Any]]:
    """
    Lists all known browser profiles and their directory paths.
    """
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    profiles_root = os.path.join(base_dir, "profiles")
    results = []

    # Check default legacy profile
    default_dir = os.path.join(base_dir, "debug_browser_profile")
    results.append({
        "name": "default",
        "path": default_dir,
        "is_default": True,
        "exists": os.path.exists(default_dir)
    })

    # Check profiles folder
    if os.path.exists(profiles_root) and os.path.isdir(profiles_root):
        for item in sorted(os.listdir(profiles_root)):
            item_path = os.path.join(profiles_root, item)
            if os.path.isdir(item_path):
                results.append({
                    "name": item,
                    "path": item_path,
                    "is_default": False,
                    "exists": True
                })
    return results


class BrowserProcessManager:
    """
    Manages browser process lifecycle (launch, port binding, profile isolation, termination).
    """
    # Footprint profiles: 'full' (default behaviour), 'light' (asset-thrifty),
    # 'hyper' (aggressive: images off, single-digit renderer/memory budgets).
    # Audits/Network/Runtime/Debugger stay fully active in every profile --
    # no flag here disables a CDP domain.
    LIGHT_FLAGS = [
        "--disable-features=OptimizationGuideModelDownloading,OptimizationHintsFetching,Translate",
        "--blink-settings=imagesEnabled=false",
        "--renderer-process-limit=2",
        "--disable-background-networking",
        "--mute-audio",
    ]
    HYPER_FLAGS = [
        "--blink-settings=imagesEnabled=false",
        "--renderer-process-limit=1",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--mute-audio",
        "--js-flags=--max-old-space-size=256",
    ]

    def __init__(
        self,
        port: int = 9223,
        headless: bool = False,
        profile: Optional[str] = None,
        profile_dir: Optional[str] = None,
        browser_bin: Optional[str] = None,
        launch_profile: str = "full"
    ):
        self.port = port
        self.headless = headless
        self.launch_profile = (launch_profile or os.environ.get("AGENTBROWSER_LAUNCH_PROFILE") or "full").lower()
        self.browser_bin = browser_bin or find_system_browser()
        if not self.browser_bin:
            raise FileNotFoundError("Could not find Chrome or Edge executable on this machine.")

        self.profile_name, self.profile_dir = resolve_profile_dir(profile=profile, profile_dir=profile_dir)
        self.proc: Optional[subprocess.Popen] = None
        self.attached_existing = False
        self._launch_attempts = 40

    def _version_payload(self) -> Optional[Dict[str, Any]]:
        try:
            r = requests.get(f"http://127.0.0.1:{self.port}/json/version", timeout=1.0)
            if r.status_code != 200:
                return None
            data = r.json()
            if "webSocketDebuggerUrl" not in data:
                return None
            return data
        except Exception:
            return None

    def is_port_ready(self) -> bool:
        return self._version_payload() is not None

    def get_ws_debugger_url(self) -> str:
        data = self._version_payload()
        if not data:
            raise RuntimeError(f"DevTools endpoint on port {self.port} is not ready.")
        url = data.get("webSocketDebuggerUrl")
        if not url:
            raise RuntimeError("Target returned 200 but webSocketDebuggerUrl was empty.")
        return url

    def _owner_matches_profile(self, version: Dict[str, Any]) -> bool:
        """
        Prove the browser already on this port belongs to this profile.

        Some Chrome builds report the user-data directory as ``userDataDir``
        on /json/version; many do not (that field is not guaranteed). The
        reliable on-disk proof is ``<profile>/DevToolsActivePort``, whose
        first line is the debugging port Chrome actually bound for that
        exact user-data-dir. Accept either proof; refuse when neither holds,
        so a port owned by a different profile is never attached.
        """
        reported = version.get("userDataDir") or version.get("user-data-dir")
        if reported:
            return os.path.normcase(os.path.abspath(reported)) == os.path.normcase(os.path.abspath(self.profile_dir))
        try:
            with open(os.path.join(self.profile_dir, "DevToolsActivePort"), "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            return bool(lines) and int(lines[0].strip()) == int(self.port)
        except Exception:
            return False

    def _kill_started_process(self):
        if not self.proc:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
                self.proc.wait(timeout=2)
            except Exception:
                pass
        self.proc = None

    def launch(self, url: str = "about:blank") -> str:
        existing = self._version_payload()
        if existing is not None:
            if not self._owner_matches_profile(existing):
                reported = existing.get("userDataDir") or "<unknown>"
                raise RuntimeError(
                    f"Debug port {self.port} is already owned by a different profile "
                    f"({reported}). Refusing to attach. Requested profile: {self.profile_dir}"
                )
            self.attached_existing = True
            return existing["webSocketDebuggerUrl"]

        os.makedirs(self.profile_dir, exist_ok=True)
        cmd = [
            self.browser_bin,
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=http://127.0.0.1",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=OptimizationGuideModelDownloading,OptimizationHintsFetching",
        ]
        if self.launch_profile == "light":
            cmd.extend(self.LIGHT_FLAGS)
        elif self.launch_profile == "hyper":
            cmd.extend(self.HYPER_FLAGS)
        if self.headless:
            cmd.extend(["--headless=new", "--disable-gpu"])
        cmd.append(url)

        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        # Readiness: Chrome writes <profile>/DevToolsActivePort the moment the
        # debugging endpoint is bound, long before an HTTP poll would notice
        # at 250ms granularity. Poll the file at 25ms, falling back to the
        # /json/version probe periodically (and for browsers we did not launch).
        active_port_file = os.path.join(self.profile_dir, "DevToolsActivePort")
        total_checks = max(1, self._launch_attempts * 10)
        for check in range(total_checks):
            if self.proc.poll() is not None:
                self._kill_started_process()
                raise RuntimeError(
                    f"Browser process exited before DevTools was ready on port {self.port}."
                )
            if os.path.exists(active_port_file) or check % 10 == 0:
                ready = self._version_payload()
                if ready is not None:
                    return ready["webSocketDebuggerUrl"]
            time.sleep(0.025)

        self._kill_started_process()
        raise TimeoutError(f"Browser did not open DevTools endpoint on port {self.port} within timeout.")

    def stop(self):
        self._kill_started_process()

        # Wait for port to be completely released
        for _ in range(15):
            if not self.is_port_ready():
                break
            time.sleep(0.2)
