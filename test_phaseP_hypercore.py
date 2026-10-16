"""
Phase P tests: fast codec, ring-buffer accounting, pipe framing, lightweight
launch profiles. No browser is launched.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser import codec
from agent_browser.cdp import BrowserProcessManager
from agent_browser.ringbuffer import EventRingBuffer
from agent_browser.transport import PipeTransport, launch_pipe_chrome, pipe_supported


def test_codec_roundtrip_and_fast_backend():
    obj = {"id": 7, "method": "Network.enable", "params": {"nested": [1, 2, {"a": "b"}]}, "sessionId": "s1"}
    assert codec.loads(codec.dumps(obj)) == obj
    assert codec.loads(codec.dumps(obj).encode()) == obj
    # msgspec is an installed extra in this environment; stdlib fallback also OK.
    assert codec.FAST_CODEC in ("msgspec", "stdlib")


@pytest.mark.asyncio
async def test_ring_buffer_never_sheds_critical_and_counts_drops():
    buf = EventRingBuffer(capacity=3)
    assert buf.put({"n": 1})
    assert buf.put({"n": 2})
    assert buf.put({"n": 3})
    assert buf.put({"n": 4}) is False  # non-critical shed at capacity
    assert buf.dropped == 1
    assert buf.put({"n": 5}, critical=True) is True  # Fetch pause is never shed
    assert await buf.get() == {"n": 1}
    assert buf.stats()["enqueued"] == 4


@pytest.mark.asyncio
async def test_pipe_transport_nul_framing():
    reader = asyncio.StreamReader()
    reader.feed_data(codec.dumps({"id": 1, "result": {}}).encode() + b"\x00" + codec.dumps({"method": "Network.requestWillBeSent", "params": {}}).encode() + b"\x00")

    class _Writer:
        def __init__(self):
            self.data = bytearray()

        def write(self, payload):
            self.data.extend(payload)

        async def drain(self):
            pass

        def close(self):
            pass

    writer = _Writer()
    transport = PipeTransport(reader, writer)
    await transport.send_frame({"id": 9, "method": "Page.enable"})
    assert bytes(writer.data).endswith(b"\x00")
    assert await transport.recv_frame() == {"id": 1, "result": {}}
    frame = await transport.recv_frame()
    assert frame["method"] == "Network.requestWillBeSent"


@pytest.mark.asyncio
async def test_pipe_launch_clear_error_on_windows():
    if pipe_supported():
        pytest.skip("POSIX host: pipe launch is supported")
    with pytest.raises(RuntimeError, match="WebSocket transport"):
        await launch_pipe_chrome("chrome", "/tmp/profile")


def test_owner_proof_via_devtools_active_port(tmp_path):
    # /json/version does not guarantee userDataDir; the profile's own
    # DevToolsActivePort file (port on line 1) is the reliable proof.
    profile = tmp_path / "prof"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text("9333\n/devtools/browser/abc", encoding="utf-8")
    mgr = BrowserProcessManager(port=9333, headless=True, profile_dir=str(profile),
                                browser_bin=str(tmp_path / "chrome.exe"))
    assert mgr._owner_matches_profile({"webSocketDebuggerUrl": "ws://x"}) is True
    mgr_other = BrowserProcessManager(port=9444, headless=True, profile_dir=str(profile),
                                      browser_bin=str(tmp_path / "chrome.exe"))
    assert mgr_other._owner_matches_profile({"webSocketDebuggerUrl": "ws://x"}) is False
    empty = tmp_path / "empty"
    empty.mkdir()
    mgr_none = BrowserProcessManager(port=9333, headless=True, profile_dir=str(empty),
                                     browser_bin=str(tmp_path / "chrome.exe"))
    assert mgr_none._owner_matches_profile({"webSocketDebuggerUrl": "ws://x"}) is False


def test_lightweight_launch_profiles(monkeypatch, tmp_path):
    captured = {}

    class _Proc:
        returncode = 1

        def poll(self):
            return 1

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 1

    def _popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr("agent_browser.cdp.subprocess.Popen", _popen)
    monkeypatch.setattr("agent_browser.cdp.time.sleep", lambda *_: None)

    for profile, needle in (("full", None), ("light", "imagesEnabled=false"), ("hyper", "--max-old-space-size")):
        mgr = BrowserProcessManager(port=1, headless=True, profile_dir=str(tmp_path / profile),
                                    browser_bin=str(tmp_path / "chrome.exe"), launch_profile=profile)
        mgr._launch_attempts = 1
        mgr._version_payload = lambda: None
        with pytest.raises((TimeoutError, RuntimeError)):
            mgr.launch()
        cmd = captured["cmd"]
        if needle is None:
            assert not any("imagesEnabled" in part for part in cmd)
        else:
            assert any(needle in part for part in cmd), (profile, cmd)
