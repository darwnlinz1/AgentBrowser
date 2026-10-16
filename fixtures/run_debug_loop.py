"""Drive the shipped CLI through one shared human/AI debug loop.

Uses python -m agent_browser against a daemon started from this tree.
Ports are chosen by the caller. Does not touch 9223/9224.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def port_open(port: int) -> bool:
    sock = socket.socket()
    sock.settimeout(0.4)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()


def run_cli(server, browser_port, profile, *args, timeout=60):
    cmd = [
        sys.executable, "-m", "agent_browser",
        "--server", server,
        "--port", str(browser_port),
        "--profile", profile,
        "--headless",
        *args,
    ]
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def main():
    server = sys.argv[1]
    browser_port = int(sys.argv[2])
    fixture_port = int(sys.argv[3])
    profile = sys.argv[4]
    out_path = sys.argv[5]

    fixture = f"http://127.0.0.1:{fixture_port}/"
    lines = []

    def record(label, code, stdout, stderr):
        lines.append(f"===== {label} exit={code} =====")
        lines.append(stdout or "")
        if stderr:
            lines.append("--- stderr ---")
            lines.append(stderr)

    code, out, err = run_cli(server, browser_port, profile, "start", "--headless", "--profile", profile, "--port", str(browser_port), "--server", server)
    record("start", code, out, err)
    if code != 0:
        _finish(out_path, lines, 1)

    code, out, err = run_cli(server, browser_port, profile, "status")
    record("status", code, out, err)

    code, out, err = run_cli(server, browser_port, profile, "open", "http://127.0.0.1:1/")
    record("open-unsafe", code, out, err)

    code, out, err = run_cli(server, browser_port, profile, "open", fixture)
    record("open-fixture", code, out, err)
    if code != 0:
        _finish(out_path, lines, 1)
    if '"navigated": true' not in (out or ""):
        lines.append("FIXTURE_DID_NOT_LOAD=True")
        _finish(out_path, lines, 1)

    code, out, err = run_cli(server, browser_port, profile, "view")
    record("view", code, out, err)
    click_ref = None
    for line in (out or "").splitlines():
        match = re.search(r"\[#(\d+)\].*Fetch status", line)
        if match:
            click_ref = match.group(1)
            break
    if click_ref is None:
        for line in (out or "").splitlines():
            if "button" in line and "[#" in line:
                match = re.search(r"\[#(\d+)\]", line)
                if match:
                    click_ref = match.group(1)
                    break
    lines.append(f"CHOSEN_REF={click_ref}")

    if click_ref:
        code, out, err = run_cli(
            server, browser_port, profile, "trace", "click",
            "--target", f"#{click_ref}", "--settle", "1.5", timeout=40,
        )
        record("click", code, out, err)
        try:
            click_json = json.loads(out)
        except Exception:
            click_json = {}
        mutations = ((click_json.get("dom_changes") or {}).get("mutation_count")) or 0
        exceptions = ((click_json.get("runtime") or {}).get("exceptions_count")) or 0
        triggered = ((click_json.get("network") or {}).get("requests_triggered_count"))
        listener = (click_json.get("action") or {}).get("listener_reached")
        page_idle = (click_json.get("action") or {}).get("page_idle")
        lines.append(f"CLICK_MUTATIONS={mutations}")
        lines.append(f"CLICK_EXCEPTIONS={exceptions}")
        lines.append(f"LISTENER_REACHED={listener}")
        lines.append(f"PAGE_IDLE={page_idle}")
        lines.append(f"REQUESTS_TRIGGERED_COUNT={triggered}")
        ping_triggered = any(
            "background-ping" in (r.get("url") or "")
            for r in ((click_json.get("network") or {}).get("requests") or [])
            if r.get("confidence") != "temporal"
        )
        lines.append(f"BACKGROUND_PING_NOT_TRIGGERED={not ping_triggered}")
        if triggered not in (0, None) and any(
            "background-ping" in (link.get("evidence") or {}).get("url", "")
            for link in ((click_json.get("evidence") or {}).get("causal_links") or [])
            if link.get("confidence") == "supported"
        ):
            lines.append("BACKGROUND_PING_COUNTED_TRIGGERED=True")
            _finish(out_path, lines, 1)
        if triggered not in (0, None):
            # A weak null-consumer link must not inflate the trigger count.
            supported = [
                link for link in ((click_json.get("evidence") or {}).get("causal_links") or [])
                if link.get("confidence") == "supported"
            ]
            if triggered != len({(link.get("evidence") or {}).get("request_id") for link in supported}):
                lines.append("TRIGGER_COUNT_MISMATCH=True")
                _finish(out_path, lines, 1)
        if mutations < 1 or exceptions < 1 or listener is not True or page_idle is not False:
            lines.append("CLICK_DID_NOT_REACH_LISTENER=True")
            _finish(out_path, lines, 1)

    time.sleep(0.4)
    code, out, err = run_cli(
        server, browser_port, profile, "eval",
        "document.getElementById('result').textContent",
    )
    record("post-click-state", code, out, err)
    lines.append(f"POST_CLICK_SAW_CLICKED={'clicked' in (out or '')}")
    if "clicked" not in (out or ""):
        lines.append("POST_CLICK_STATE_MISSING=True")
        _finish(out_path, lines, 1)

    code, out, err = run_cli(server, browser_port, profile, "user-events", "--limit", "20")
    record("user-events", code, out, err)
    human_ok = False
    try:
        events = json.loads(out).get("actions") or []
        for action in events:
            element = action.get("element") or {}
            pixel = action.get("pixel") or {}
            if (
                action.get("type") == "click"
                and element.get("id") == "fetch-btn"
                and element.get("selector")
                and action.get("time")
                and pixel.get("x", 0) > 0
                and pixel.get("y", 0) > 0
            ):
                human_ok = True
    except Exception:
        human_ok = False
    lines.append(f"HUMAN_EVENT_OK={human_ok}")
    if not human_ok:
        _finish(out_path, lines, 1)

    code, out, err = run_cli(server, browser_port, profile, "ai-traffic", "--limit", "30")
    record("ai-traffic", code, out, err)
    lines.append(f"AI_HAS_ENCRYPTED_URL={'/api/encrypted' in (out or '')}")
    lines.append(f"AI_REDACTED={'[redacted]' in (out or '')}")
    lines.append(f"AI_LEAKED_AUTH={'fixture-secret-token' in (out or '')}")
    lines.append(f"AI_LEAKED_COOKIE={'fixture-secret-cookie' in (out or '')}")
    if "/api/encrypted" not in (out or "") or "[redacted]" not in (out or "") or "fixture-secret-token" in (out or "") or "fixture-secret-cookie" in (out or ""):
        lines.append("AI_SUMMARY_FAILED=True")
        _finish(out_path, lines, 1)

    code, out, err = run_cli(server, browser_port, profile, "diagnose")
    record("diagnose", code, out, err)

    # Find the encrypted request id from traffic JSON.
    code, tout, terr = run_cli(server, browser_port, profile, "traffic", "--search", "encrypted", "--limit", "10")
    record("traffic-encrypted", code, tout, terr)
    enc_id = None
    try:
        traffic = json.loads(tout)
        if isinstance(traffic, list) and traffic:
            enc_id = traffic[0].get("id")
    except Exception:
        enc_id = None
    lines.append(f"ENC_REQUEST_ID={enc_id}")
    if enc_id:
        time.sleep(0.4)
        code, out, err = run_cli(server, browser_port, profile, "caller", enc_id)
        record("caller", code, out, err)
        caller_ok = "sendEncrypted" in (out or "") and "encrypt.js" in (out or "")
        body_ok = '"ok": true' in (out or "") or '"ok":true' in (out or "") or '\\"ok\\": true' in (out or "")
        lines.append(f"CALLER_NAMES_ENCRYPT={caller_ok}")
        lines.append(f"CAPTURED_RESPONSE_BODY={body_ok}")
        if not caller_ok or not body_ok:
            _finish(out_path, lines, 1)

    code, out, err = run_cli(server, browser_port, profile, "diagnose")
    record("diagnose", code, out, err)

    code, out, err = run_cli(server, browser_port, profile, "crypto-logs")
    record("crypto-logs", code, out, err)

    code, out, err = run_cli(server, browser_port, profile, "find-crypto")
    record("find-crypto", code, out, err)

    code, out, err = run_cli(server, browser_port, profile, "scripts")
    record("scripts", code, out, err)
    script_id = None
    try:
        scripts = json.loads(out)
        for item in scripts.get("scripts") or []:
            if "encrypt.js" in (item.get("url") or ""):
                script_id = item.get("scriptId")
                break
    except Exception:
        script_id = None
    lines.append(f"ENCRYPT_SCRIPT_ID={script_id}")
    if script_id:
        code, out, err = run_cli(server, browser_port, profile, "script-source", script_id)
        record("script-source", code, out, err)
        lines.append(f"SCRIPT_SOURCE_OK={'function encryptPayload' in (out or '')}")
        if "function encryptPayload" not in (out or ""):
            _finish(out_path, lines, 1)

    # Intercept a matching response and let the page consume it.
    code, out, err = run_cli(
        server, browser_port, profile,
        "intercept", "add", "api/mock-ok",
        "--rule-id", "mock-ok",
        "--action", "fulfill",
        "--mock-status", "200",
        "--mock-body", '{"consumed":"intercept-body-9f3a"}',
    )
    record("intercept-add", code, out, err)
    code, out, err = run_cli(
        server, browser_port, profile, "eval",
        "fetch('/api/mock-ok').then(r => r.text())",
    )
    record("intercept-consume", code, out, err)
    lines.append(f"INTERCEPT_CONSUMED={'intercept-body-9f3a' in (out or '')}")
    if "intercept-body-9f3a" not in (out or ""):
        _finish(out_path, lines, 1)

    # Seed a replayable request, then replay it out of browser.
    code, out, err = run_cli(
        server, browser_port, profile, "eval",
        "fetch('/api/replay-target').then(r => r.text())",
    )
    record("seed-replay", code, out, err)
    time.sleep(0.4)
    code, tout, terr = run_cli(server, browser_port, profile, "traffic", "--search", "replay-target", "--limit", "5")
    record("traffic-replay", code, tout, terr)
    replay_id = None
    try:
        traffic = json.loads(tout)
        if isinstance(traffic, list) and traffic:
            replay_id = traffic[0].get("id")
    except Exception:
        replay_id = None
    lines.append(f"REPLAY_REQUEST_ID={replay_id}")
    if replay_id:
        code, out, err = run_cli(server, browser_port, profile, "replay", replay_id)
        record("replay", code, out, err)
        replay_ok = '"success": true' in (out or "") and "out-of-browser" in (out or "") and '"status": 200' in (out or "")
        lines.append(f"REPLAY_OK={replay_ok}")
        if not replay_ok:
            _finish(out_path, lines, 1)

    code, out, err = run_cli(server, browser_port, profile, "stop")
    record("stop", code, out, err)
    time.sleep(0.8)
    lines.append(f"DAEMON_PORT_OPEN={port_open(int(server.rsplit(':', 1)[-1]))}")
    lines.append(f"BROWSER_PORT_OPEN={port_open(browser_port)}")
    _finish(out_path, lines, 0)


def _finish(path, lines, code):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = "\n".join(lines)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    sys.stdout.write(text)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
