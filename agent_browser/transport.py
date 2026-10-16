"""
CDP transport layer.

Two transports share one frame contract (one JSON object per frame):

- ``WebSocketTransport`` -- the standard ``--remote-debugging-port`` endpoint.
  Works everywhere, costs an HTTP upgrade + WebSocket framing.
- ``PipeTransport`` -- Chrome's ``--remote-debugging-pipe``: CDP over inherited
  file descriptors with NUL-delimited JSON, no HTTP/WebSocket layer at all
  (the mode Puppeteer's ``pipe: true`` uses). This is the zero-copy path:
  frames go straight into the codec. POSIX supports fd inheritance directly;
  on Windows, descriptor inheritance for the pipe endpoints is not exposed by
  the stdlib subprocess API, so pipe mode raises a clear error and callers
  fall back to WebSocket rather than failing obscurely.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, AsyncIterator, Callable, Optional

from agent_browser import codec


class PipeTransport:
    """NUL-framed CDP over a duplex byte stream (reader/writer pair)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._buf = bytearray()

    async def send_frame(self, obj: Any) -> None:
        payload = codec.dumps(obj).encode("utf-8") + b"\x00"
        self._writer.write(payload)
        await self._writer.drain()

    async def recv_frame(self) -> Optional[Any]:
        while True:
            idx = self._buf.find(b"\x00")
            if idx >= 0:
                frame = bytes(self._buf[:idx])
                del self._buf[: idx + 1]
                if frame:
                    return codec.loads(frame)
                continue
            chunk = await self._reader.read(65536)
            if not chunk:
                return None
            self._buf.extend(chunk)

    async def frames(self) -> AsyncIterator[Any]:
        while True:
            frame = await self.recv_frame()
            if frame is None:
                return
            yield frame

    async def close(self) -> None:
        try:
            self._writer.close()
        except Exception:
            pass


def pipe_supported() -> bool:
    """``--remote-debugging-pipe`` needs pass_fds-style inheritance (POSIX)."""
    return os.name == "posix"


class PipeCDPClient:
    """
    Minimal CDP client over inherited pipe descriptors (thread IO).

    Chrome's side of ``--remote-debugging-io-pipes`` is synchronous, so the
    parent side uses a blocking reader thread + executor writes instead of
    asyncio pipe transports (which reject these handles on Windows). Replies
    resolve futures on the event loop; events go to sync handlers inline in
    the reader thread (they must be thread-safe and O(1)) and to async
    handlers via ``run_coroutine_threadsafe``.
    """

    def __init__(self, write_fd: int, read_fd: int):
        self._w = write_fd
        self._r = read_fd
        self._cmd_id = 0
        self._pending: dict = {}
        self._handlers: list = []
        self._sync_handlers: list = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[Any] = None
        self.closed = False
        # CDPConnection-compatible surface (subset used by the controller).
        self.generation = 1
        self.dropped_events = 0
        self.skipped_events = 0
        self.lane_enqueued = 0
        self.lane_spilled = 0
        self._lane_maxsize = 1024
        self._lanes: dict = {}
        self._lane_workers: dict = {}
        self.sync_handler_errors = 0
        self.on_drop = None
        self._sync_interest: dict = {}
        self._event_interest: dict = {}

    @staticmethod
    def _resolve_interest(handler, methods):
        if methods is not None:
            return frozenset(methods)
        declared = getattr(handler, "cdp_methods", None)
        return frozenset(declared) if declared else None

    def _interested(self, handlers, interest, method):
        return [h for h in handlers
                if interest.get(h) is None or method in interest[h]]

    def add_event_handler(self, handler, methods=None) -> None:
        self._handlers.append(handler)
        self._event_interest[handler] = self._resolve_interest(handler, methods)

    def remove_event_handler(self, handler) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)
        self._event_interest.pop(handler, None)

    def add_sync_handler(self, handler, methods=None) -> None:
        self._sync_handlers.append(handler)
        self._sync_interest[handler] = self._resolve_interest(handler, methods)

    def remove_sync_handler(self, handler) -> None:
        if handler in self._sync_handlers:
            self._sync_handlers.remove(handler)
        self._sync_interest.pop(handler, None)

    async def connect(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return  # idempotent: spawn_* already connected us
        self._loop = asyncio.get_running_loop()
        import threading

        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        import json as _json

        buf = bytearray()
        try:
            while not self.closed:
                chunk = os.read(self._r, 65536)
                if not chunk:
                    break
                buf.extend(chunk)
                idx = buf.find(b"\x00")
                while idx >= 0:
                    frame = bytes(buf[:idx])
                    del buf[: idx + 1]
                    if frame:
                        try:
                            envelope = codec.decode_envelope(frame)
                            if envelope is not None and envelope.method:
                                interested = (
                                    self._interested(self._sync_handlers, self._sync_interest, envelope.method)
                                    + self._interested(self._handlers, self._event_interest, envelope.method))
                                if not interested:
                                    self.skipped_events += 1
                                    idx = buf.find(b"\x00")
                                    continue
                            self._dispatch(_json.loads(frame))
                        except Exception:
                            pass
                    idx = buf.find(b"\x00")
        except Exception:
            pass
        finally:
            self.closed = True
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._close_lanes)
                for fut in list(self._pending.values()):
                    if not fut.done():
                        self._loop.call_soon_threadsafe(fut.set_exception, ConnectionError("pipe closed"))
                cb = self.on_drop
                if cb:
                    try:
                        cb(self.generation)
                    except Exception:
                        pass

    async def _lane_loop(self, key: str) -> None:
        queue = self._lanes[key]
        while True:
            coro = await queue.get()
            try:
                await coro
            except Exception:
                pass
            finally:
                queue.task_done()

    def _close_lanes(self) -> None:
        """Loop-thread: cancel lane workers and drop queued handler calls."""
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
                    self.dropped_events += 1
        self._lanes.clear()

    def _lane_put(self, key: str, handler, obj: dict) -> None:
        """Loop-thread: enqueue one handler call into its session FIFO lane."""
        try:
            coro = handler(obj)
        except Exception:
            return
        if not asyncio.iscoroutine(coro):
            return
        queue = self._lanes.get(key)
        if queue is None:
            queue = asyncio.Queue(maxsize=self._lane_maxsize)
            self._lanes[key] = queue
            self._lane_workers[key] = asyncio.ensure_future(self._lane_loop(key))
        try:
            queue.put_nowait(coro)
            self.lane_enqueued += 1
        except asyncio.QueueFull:
            self.lane_spilled += 1
            asyncio.ensure_future(coro)

    def _dispatch(self, obj: dict) -> None:
        mid = obj.get("id")
        if mid is not None and mid in self._pending:
            fut = self._pending.pop(mid)
            if self._loop is not None and not fut.done():
                self._loop.call_soon_threadsafe(fut.set_result, obj)
            return
        if obj.get("method"):
            for fn in self._interested(self._sync_handlers, self._sync_interest, obj["method"]):
                try:
                    fn(obj)
                except Exception:
                    self.sync_handler_errors += 1
            if self._loop is not None:
                key = obj.get("sessionId") or ""
                for handler in self._interested(self._handlers, self._event_interest, obj["method"]):
                    self._loop.call_soon_threadsafe(self._lane_put, key, handler, obj)

    async def send(self, method: str, params: Optional[dict] = None,
                   session_id: Optional[str] = None, timeout: float = 15.0) -> dict:
        if self.closed:
            raise ConnectionError("pipe transport is closed")
        self._cmd_id += 1
        cid = self._cmd_id
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[cid] = fut
        msg: dict = {"id": cid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        payload = (codec.dumps(msg) + "\x00").encode("utf-8")
        await loop.run_in_executor(None, os.write, self._w, payload)
        try:
            res = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(cid, None)
        if "error" in res:
            raise RuntimeError(f"CDP Error [{method}]: {res['error'].get('message', res['error'])}")
        return res.get("result", {})

    async def disconnect(self) -> None:
        self.closed = True
        self._close_lanes()
        try:
            os.close(self._w)
        except Exception:
            pass

    async def close(self) -> None:
        await self.disconnect()


async def spawn_chrome_pipe_windows(browser_bin: str, profile_dir: str,
                                    extra_flags: Optional[list] = None,
                                    url: str = "about:blank"):
    """
    Launch Chrome on Windows with ``--remote-debugging-pipe`` +
    ``--remote-debugging-io-pipes`` (Chrome 120+). Returns (proc, PipeCDPClient).

    No TCP port is opened at all, and Chrome exits when the pipe closes.
    Verified live against Chrome 155 on Windows 10.
    """
    if os.name != "nt":
        raise RuntimeError("spawn_chrome_pipe_windows is Windows-only; use launch_pipe_chrome on POSIX")
    import _winapi
    import msvcrt
    import subprocess
    import threading
    from asyncio import windows_utils as wu

    to_chrome_r, to_chrome_w = wu.pipe(overlapped=(False, False), bufsize=256 << 10)
    from_chrome_r, from_chrome_w = wu.pipe(overlapped=(False, False), bufsize=1 << 20)
    child_ends = [to_chrome_r, from_chrome_w]
    lock = threading.Lock()
    with lock:
        for h in child_ends:
            os.set_handle_inheritable(h, True)
        si = subprocess.STARTUPINFO()
        si.lpAttributeList = {"handle_list": child_ends}
        try:
            proc = subprocess.Popen(
                [browser_bin, "--remote-debugging-pipe",
                 f"--remote-debugging-io-pipes={to_chrome_r},{from_chrome_w}",
                 f"--user-data-dir={profile_dir}",
                 "--no-first-run", "--no-default-browser-check",
                 *(extra_flags or []), url],
                startupinfo=si, close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        finally:
            for h in child_ends:
                _winapi.CloseHandle(h)
    w_fd = msvcrt.open_osfhandle(to_chrome_w, os.O_WRONLY | os.O_BINARY)
    r_fd = msvcrt.open_osfhandle(from_chrome_r, os.O_RDONLY | os.O_BINARY)
    client = PipeCDPClient(w_fd, r_fd)
    await client.connect()
    return proc, client


async def launch_pipe_chrome(
    browser_bin: str,
    user_data_dir: str,
    extra_flags: Optional[list] = None,
    url: str = "about:blank",
):
    """
    Launch Chrome with ``--remote-debugging-pipe`` and return
    ``(process, PipeTransport)`` speaking CDP on fds 3 (in) / 4 (out):
    the parent writes commands to fd 3 and reads replies from fd 4,
    NUL-delimited JSON, no HTTP/WebSocket layer.

    Raises RuntimeError on Windows, where stdlib subprocess cannot inherit
    the pipe descriptors -- use the WebSocket transport there.
    """
    if not pipe_supported():
        raise RuntimeError(
            "--remote-debugging-pipe requires POSIX fd inheritance; "
            "use the WebSocket transport (--remote-debugging-port) on Windows."
        )
    import subprocess

    in_r, in_w = os.pipe()    # parent -> Chrome (becomes fd 3 in the child)
    out_r, out_w = os.pipe()  # Chrome -> parent (becomes fd 4 in the child)

    def _preexec():
        os.dup2(in_r, 3)
        os.dup2(out_w, 4)

    cmd = [
        browser_bin,
        "--remote-debugging-pipe",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        *(extra_flags or []),
        url,
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(in_r, out_w),
        preexec_fn=_preexec,
        close_fds=True,
    )
    os.close(in_r)
    os.close(out_w)

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    read_file = os.fdopen(out_r, "rb")
    await loop.connect_read_pipe(lambda: protocol, read_file)
    write_file = os.fdopen(in_w, "wb")
    writer_transport, _ = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, write_file
    )
    writer = asyncio.StreamWriter(writer_transport, protocol, reader, loop)
    return proc, PipeTransport(reader, writer)
