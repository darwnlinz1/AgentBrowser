"""
Hardened-daemon CLI smoke: real subprocesses, ephemeral ports only.

Verifies the Phase 0 daemon contract end to end:
- /session/status reachable without a token (health probe);
- POST /session/start without the bearer token -> 401;
- cross-site Origin -> 403 (DNS-rebinding / browser-driven defense);
- the CLI (which reads the per-port token file) can start, open, view,
  and click -- with the canonical attributed trace -- then stop, and the
  daemon process exits.

Usage: python -m fixtures.run_daemon_smoke
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_browser.session_registry import find_free_port
from fixtures.fixture_server import FixtureServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def main() -> int:
    srv_port = find_free_port()
    browser_port = find_free_port()
    base = f"http://127.0.0.1:{srv_port}"
    failures = []

    def check(name, cond, detail=""):
        print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")
        if not cond:
            failures.append(name)

    def cli(*args, timeout=90):
        cmd = [sys.executable, "-m", "agent_browser", "--server", base,
               "--port", str(browser_port), "--profile", "daemon_smoke",
               "--headless", *args]
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr

    with FixtureServer() as fx:
        daemon = subprocess.Popen(
            [sys.executable, "-m", "agent_browser.server", "--host", "127.0.0.1", "--port", str(srv_port)],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            status = None
            for _ in range(80):
                try:
                    with urllib.request.urlopen(base + "/session/status", timeout=2) as resp:
                        status = json.loads(resp.read())
                    break
                except Exception:
                    time.sleep(0.25)
            check("status open path", status is not None and status.get("active") is False)

            req = urllib.request.Request(base + "/session/start", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=5)
                check("unauth start rejected", False, "(was accepted)")
            except urllib.error.HTTPError as e:
                check("unauth start rejected", e.code == 401, f"({e.code})")

            req = urllib.request.Request(base + "/session/status",
                                         headers={"Origin": "https://evil.example.com"})
            try:
                urllib.request.urlopen(req, timeout=5)
                check("evil origin rejected", False, "(was accepted)")
            except urllib.error.HTTPError as e:
                check("evil origin rejected", e.code == 403, f"({e.code})")

            rc, out, _ = cli("start")
            check("cli start", rc == 0, out.strip()[-80:])
            check("cli start profile honored", json.loads(out).get("profile_name") == "daemon_smoke",
                  str(json.loads(out).get("profile_name")))
            rc, out, _ = cli("open", fx.case_url("http500-null-deref"))
            check("cli open", rc == 0 and json.loads(out).get("navigated") is True)
            rc, out, _ = cli("view")
            check("cli view", rc == 0 and "Trigger Broken Flow" in out)
            # The fixture's background ping fires every 400ms while the
            # tracer's quiet window is 250ms, so a trace can legitimately
            # settle in the gap between pings on a loaded runner. Retry
            # the click (same full oracle per attempt) instead of racing
            # the ping period once.
            attribution_ok = False
            attribution_detail = ""
            for _attempt in range(3):
                rc, out, _ = cli("click", "#go")
                data = json.loads(out)
                by_path = {r["url"].rsplit("/", 1)[-1]: r for r in data["network"]["requests"]}
                attribution_detail = str([(k, v.get("confidence")) for k, v in by_path.items()])
                attribution_ok = (
                    by_path.get("failing-endpoint", {}).get("confidence") == "attributed"
                    and by_path.get("background-ping", {}).get("confidence") == "temporal"
                    and data["runtime"]["exceptions_count"] == 1)
                if attribution_ok:
                    break
            check("cli click attribution", attribution_ok, attribution_detail)
            rc, out, _ = cli("stop")
            check("cli stop", rc == 0)
            # Graceful shutdown closes a real browser; on a loaded CI
            # runner that can take well over a second. Poll for exit
            # instead of sleeping a fixed 1s (the assertion is unchanged:
            # the daemon MUST exit after stop).
            for _ in range(60):
                if daemon.poll() is not None:
                    break
                time.sleep(0.25)
            check("daemon exited", daemon.poll() is not None)
        finally:
            if daemon.poll() is None:
                daemon.terminate()

    print("\nDAEMON SMOKE", "PASS" if not failures else f"FAILURES: {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
