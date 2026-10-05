import os
import sys
import time
import json
import subprocess
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler

from test_suite import start_mock_server

def run_cmd(cmd_list):
    print(f"[*] Executing: {' '.join(cmd_list)}")
    res = subprocess.run(cmd_list, capture_output=True, text=True, timeout=30)
    print("STDOUT:", res.stdout.strip())
    if res.stderr.strip():
        print("STDERR:", res.stderr.strip())
    assert res.returncode == 0, f"Command failed with code {res.returncode}"
    return res.stdout

def test_cli():
    server = start_mock_server(8898)
    try:
        # 1. Start browser via CLI
        out = run_cmd([sys.executable, "-m", "agent_browser", "start", "--headless", "--port", "9226"])
        time.sleep(1)

        # 2. Check status
        out = run_cmd([sys.executable, "-m", "agent_browser", "status"])
        status = json.loads(out)
        assert status.get("active") is True

        # 3. Open URL
        run_cmd([sys.executable, "-m", "agent_browser", "open", "http://127.0.0.1:8898"])

        # 4. View
        out = run_cmd([sys.executable, "-m", "agent_browser", "view"])
        assert "Debugger Test Page" in out

        # 5. Click
        out = run_cmd([sys.executable, "-m", "agent_browser", "click", "#btn-mutate"])
        report = json.loads(out)
        assert report["action"]["success"] is True

        # 6. Eval
        out = run_cmd([sys.executable, "-m", "agent_browser", "eval", "document.getElementById('content').textContent"])
        res_eval = json.loads(out)
        assert "DOM Successfully Mutated" in res_eval["value"]

        # 7. Stop
        run_cmd([sys.executable, "-m", "agent_browser", "stop"])
        print("\n[+] CLI VERIFICATION PASSED COMPLETELY!")

    finally:
        server.shutdown()

if __name__ == "__main__":
    test_cli()
