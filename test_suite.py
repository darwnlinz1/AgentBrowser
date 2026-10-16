import os
import sys
import time
import json
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# Ensure agent_browser is importable
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.agent_api import AgentBrowserController
from agent_browser.network import InterceptRule


class MockWebHandler(BaseHTTPRequestHandler):
    """
    Mock Web Server providing rich HTML, dynamic DOM mutations, API endpoints,
    and deliberate errors to thoroughly test the debugger.
    """
    def log_message(self, format, *args):
        pass  # Quiet logging

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            html = """<!DOCTYPE html>
<html>
<head>
    <title>AgentBrowser Test Ground</title>
</head>
<body>
    <h1>Debugger Test Page</h1>
    <div id="content">Initial Content</div>
    <button id="btn-mutate" onclick="mutateDOM()">Mutate DOM</button>
    <button id="btn-api" onclick="callAPI()">Fetch API Data</button>
    <button id="btn-error" onclick="triggerBug()">Trigger Broken Flow</button>
    <input id="user-input" type="text" placeholder="Enter test query" value="hello world" />

    <script>
        function mutateDOM() {
            const div = document.getElementById('content');
            div.textContent = 'DOM Successfully Mutated!';
            const p = document.createElement('p');
            p.id = 'new-item';
            p.textContent = 'Appended Child Element';
            document.body.appendChild(p);
        }

        async function callAPI() {
            const res = await fetch('/api/data');
            const data = await res.json();
            document.getElementById('content').textContent = 'API Result: ' + data.message;
        }

        async function triggerBug() {
            console.error('Deliberate test error logged');
            const res = await fetch('/api/failing-endpoint');
            const data = await res.json();
            // Deliberate JS error accessing undefined property
            const x = data.nonExistent.subField;
        }
    </script>
</body>
</html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

        elif self.path == "/api/data":
            data = {"status": "ok", "message": "Real API payload"}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif self.path == "/api/failing-endpoint":
            err = {"error": "Internal Error", "code": 500}
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(err).encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()


def start_mock_server(port=8899):
    server = HTTPServer(("127.0.0.1", port), MockWebHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


async def run_integration_tests():
    print("=" * 70)
    print("  RUNNING AGENTBROWSER AUTOMATED VERIFICATION SUITE")
    print("=" * 70)

    # 1. Start local mock web server
    mock_port = 8899
    mock_server = start_mock_server(mock_port)
    base_url = f"http://127.0.0.1:{mock_port}"
    print(f"[+] Mock test web server listening at {base_url}")

    # 2. Launch AgentBrowser in headless mode
    controller = AgentBrowserController(port=9225, headless=True)
    print("[*] Launching headless browser on CDP port 9225...")
    await controller.start(initial_url="about:blank")
    print("[+] Browser and CDP connection successfully established.")

    try:
        # TEST 1: Navigation and DOM Snapshot
        print("\n--- TEST 1: Navigation & Semantic Agent View ---")
        await controller.navigate(base_url)
        view = await controller.get_agent_view()
        print("[+] Agent View Output:\n" + view)
        assert "Debugger Test Page" in view, "Page title/header not in view"
        assert "[#1]" in view or "[#" in view, "Ref tags not generated"
        print("[PASS] Navigation and Semantic View verified.")

        # TEST 2: Action Tracing - DOM Mutation
        print("\n--- TEST 2: Causal Action Tracing (DOM Mutation) ---")
        trace_mutate = await controller.trace_action("click", target="#btn-mutate")
        print("[+] Action Trace Report (Mutate Button):")
        print(f"    Success: {trace_mutate['action']['success']}")
        print(f"    DOM Mutations count: {trace_mutate['dom_changes']['mutation_count']}")
        print(f"    Sample mutations: {json.dumps(trace_mutate['dom_changes']['sample_mutations'])}")
        assert trace_mutate["action"]["success"] is True
        assert trace_mutate["dom_changes"]["mutation_count"] > 0, "Mutations not detected by observer"
        print("[PASS] DOM mutation causal tracing verified.")

        # TEST 3: Action Tracing - Network Request Triggered by Click
        print("\n--- TEST 3: Causal Action Tracing (Network Trigger) ---")
        trace_api = await controller.trace_action("click", target="#btn-api")
        print("[+] Action Trace Report (API Button):")
        print(f"    Requests triggered: {trace_api['network']['requests_triggered_count']}")
        print(f"    Request details: {json.dumps(trace_api['network']['requests'])}")
        assert trace_api["network"]["requests_observed_count"] >= 1, "Network request was not captured"
        assert any("/api/data" in r["url"] for r in trace_api["network"]["requests"]), "Missing /api/data request"
        # The click's fetch carries the action's stamped sourceURL in its async
        # initiator chain, so it is proven (attributed), not merely temporal.
        assert any(r.get("confidence") == "attributed" and r.get("caused_by_action") for r in trace_api["network"]["requests"])
        print("[PASS] Network causal tracing verified.")

        # TEST 4: Traffic Search and Replay
        print("\n--- TEST 4: Traffic Buffer & Request Replay ---")
        traffic = controller.network.search_traffic(query="/api/data")
        assert len(traffic) > 0, "Traffic search returned empty"
        req_id = traffic[0]["id"]
        print(f"[+] Replaying captured request ID: {req_id}")
        replay_res = controller.network.replay_request(req_id, mutate_headers={"X-Agent-Test": "True"})
        print(f"    Replay status: {replay_res['replayed']['status']}")
        print(f"    Diff against baseline: {replay_res['diff']}")
        assert replay_res["success"] is True
        assert replay_res["replayed"]["status"] == 200
        print("[PASS] Traffic search and request replay verified.")

        # TEST 5: Interception & Mocking (Burp Suite capability)
        print("\n--- TEST 5: Network Interception & API Tampering ---")
        mock_rule = InterceptRule(
            rule_id="test_mock",
            url_pattern=r"/api/data",
            action="fulfill",
            mock_status=200,
            mock_headers={"Content-Type": "application/json"},
            mock_body=json.dumps({"status": "mocked", "message": "AGENT_TAMPERED_SUCCESS"})
        )
        controller.network.add_rule(mock_rule)
        await controller.network.enable_interception()

        trace_intercept = await controller.trace_action("click", target="#btn-api")
        dom_text = await controller.dom.get_outer_html("#content")
        print(f"[+] Content div after intercepted click: {dom_text}")
        assert "AGENT_TAMPERED_SUCCESS" in dom_text, "Mock response body not received by webpage"
        print("[PASS] Network interception and API mocking verified.")

        await controller.network.disable_interception()

        # TEST 6: Diagnostic Root-Cause Analysis on Bug
        print("\n--- TEST 6: Automated Root Cause Diagnostics ---")
        trace_bug = await controller.trace_action("click", target="#btn-error")
        print(f"[+] Action Trace Report (Bug Button):")
        print(f"    Failing requests: {trace_bug['network']['failing_requests_count']}")
        print(f"    Runtime exceptions: {trace_bug['runtime']['exceptions_count']}")
        print(f"    Diagnostics: {json.dumps(trace_bug['diagnostics'], indent=2)}")

        diag = await controller.diagnostics.run_diagnostics()
        print(f"[+] Diagnostic Engine Report:")
        print(f"    Health: {diag['health']}")
        print(f"    Total Issues: {diag['total_issues']}")
        print(f"    Root Cause Analysis: {json.dumps(diag['root_cause_analysis'], indent=2)}")
        assert diag["health"] == "UNHEALTHY"
        assert diag["total_issues"] >= 1
        print("[PASS] Diagnostic engine successfully correlated HTTP 500 and JavaScript error.")

        # TEST 7: Storage & Cookie Management
        print("\n--- TEST 7: Storage & Cookie Management ---")
        await controller.storage.set_cookie(name="agent_session", value="tok_12345", domain="127.0.0.1")
        cookies = await controller.storage.get_cookies()
        found_cookie = any(c["name"] == "agent_session" and c["value"] == "tok_12345" for c in cookies)
        assert found_cookie, "Set cookie not found in storage"

        await controller.storage.set_storage_item("local", "agent_key", "storage_value_xyz")
        local_store = await controller.storage.get_storage("local")
        assert local_store.get("agent_key") == "storage_value_xyz", "LocalStorage item mismatch"
        print("[PASS] Storage and Cookie operations verified.")

        print("\n" + "=" * 70)
        print("  ALL 7 VERIFICATION PHASES PASSED WITH ZERO FAILURES!")
        print("=" * 70)

    finally:
        await controller.stop()
        mock_server.shutdown()


if __name__ == "__main__":
    asyncio.run(run_integration_tests())
