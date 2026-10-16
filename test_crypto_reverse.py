import os
import sys
import time
import json
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.agent_api import AgentBrowserController


class CryptoMockServerHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/":
            html = """<!DOCTYPE html>
<html>
<head>
    <title>AI Reverse Engineering & Crypto Test Ground</title>
</head>
<body>
    <h1>API Security & Crypto Demonstration</h1>
    <button id="btn-call-api" onclick="sendEncryptedPayload()">Send Encrypted Request</button>
    <div id="result-box">Waiting...</div>

    <!-- External Script Mock -->
    <script src="/crypto_helper.js"></script>
    <script>
        function sendEncryptedPayload() {
            const rawData = { accountId: 9988, secretToken: "spinach_secret_key_2026" };
            // Inlined encrypt logic calling helper
            const cipher = customEncryptAES(JSON.stringify(rawData));
            
            fetch('/api/v2/secure-gateway', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Signature': 'sig_mock_4920'
                },
                body: JSON.stringify({ payload: cipher })
            }).then(r => r.json()).then(data => {
                document.getElementById('result-box').textContent = "Server: " + data.reply;
            });
        }
    </script>
</body>
</html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

        elif self.path == "/crypto_helper.js":
            js = """
// Simulated Crypto Helper Library
function customEncryptAES(plainText) {
    // Basic mock transformation recognizable by regex
    const encrypted = btoa("ENCRYPTED_AES_" + plainText);
    return encrypted;
}

function verifySignature(token) {
    return "sha256_" + token;
}
"""
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.end_headers()
            self.wfile.write(js.encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/v2/secure-gateway":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length > 0 else ""
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "auth_session=granted_session_xyz; Path=/; HttpOnly")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "success",
                "received_encrypted": True,
                "reply": "DECRYPTED_OK_BY_SERVER"
            }).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


def start_test_server(port=8897):
    server = HTTPServer(("127.0.0.1", port), CryptoMockServerHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


async def run_crypto_and_source_tests():
    print("=" * 70)
    print("  RUNNING AI REVERSE ENGINEERING, SOURCE & CRYPTO TEST SUITE")
    print("=" * 70)

    server = start_test_server(8897)
    base_url = "http://127.0.0.1:8897"

    controller = AgentBrowserController(port=9241, headless=True, enable_proxy=True, proxy_port=8892)
    await controller.start()

    try:
        # Phase 1: Navigation
        print(f"[*] Navigating to {base_url}...")
        await controller.navigate(base_url)

        # Phase 2: Harvest Scripts
        print("\n--- PHASE 1: Script Harvester Verification ---")
        scripts = await controller.source_inspector.list_all_scripts()
        print(f"[+] Total scripts detected on page: {len(scripts)}")
        assert len(scripts) >= 1, "Failed to capture page scripts"

        helper_script = None
        for s in scripts:
            print(f"    Script: {s['url']} (Inline: {s['is_inline']})")
            if "crypto_helper.js" in s["url"]:
                helper_script = s

        assert helper_script is not None, "crypto_helper.js was not detected by SourceInspector"
        
        # Phase 3: Fetch Full Script Content
        print("\n--- PHASE 2: Fetching Script Source via CDP ---")
        src_code = await controller.source_inspector.get_script_source(helper_script["scriptId"])
        print(f"[+] Script source preview:\n{src_code.strip()[:200]}...")
        assert "customEncryptAES" in src_code, "Failed to retrieve customEncryptAES source code"

        # Phase 4: Find Crypto / API Functions Pattern Scanner
        print("\n--- PHASE 3: Crypto & API Keyword Static Scanner ---")
        findings = await controller.source_inspector.find_crypto_and_api_functions()
        print(f"[+] Crypto/API findings count: {len(findings)}")
        assert len(findings) >= 1, "No crypto or API patterns detected"
        for f in findings[:3]:
            print(f"    Found [{f['tag']}] '{f['match']}' in {f['url']}:{f['line']}")

        # Phase 5: Trigger Encrypted API Call & Verify Early Hook Logging
        print("\n--- PHASE 4: Action Trigger & Early Pre-encryption Hooking ---")
        await controller.dom.click("#btn-call-api")
        await asyncio.sleep(1.0)

        crypto_logs = await controller.source_inspector.collect_crypto_hook_logs()
        print(f"[+] Captured early in-page logs: {len(crypto_logs)}")
        has_fetch_hook = any(l.get("kind") == "fetch" for l in crypto_logs)
        has_stringify_hook = any(l.get("kind") == "json_stringify" for l in crypto_logs)
        print(f"    window.fetch hooked: {has_fetch_hook}")
        print(f"    JSON.stringify hooked: {has_stringify_hook}")
        assert has_fetch_hook or has_stringify_hook, "Early hooks failed to intercept plaintext before network call"

        # Phase 6: AI-Optimized Token-Lean Network Representation
        print("\n--- PHASE 5: AI-Optimized Compact Network Summary ---")
        ai_summary = controller.network.format_ai_summary(limit=10)
        print(ai_summary)
        assert "/api/v2/secure-gateway" in ai_summary, "Target API not present in AI summary"
        assert "auth_session=granted_session_xyz" in ai_summary or "Set-Cookie" in ai_summary or "Response Body" in ai_summary

        # Phase 7: Caller Call Stack Resolution for Encrypted Request
        print("\n--- PHASE 6: Request Initiator & JS Caller Resolution ---")
        reqs = controller.network.search_traffic(query="secure-gateway")
        assert len(reqs) >= 1, "Encrypted request not found in network buffer"
        target_req = reqs[0]
        caller = await controller.source_inspector.get_caller_context_for_request(target_req.get("initiator", {}))
        print(f"[+] Request Initiator Type: {caller.get('type')}")
        print(f"    Has Call Stack: {caller.get('has_stack')}")
        if caller.get("top_caller"):
            top = caller["top_caller"]
            print(f"    Top caller function: {top.get('function')} in {top.get('url')}:{top.get('line')}")
            if top.get("code_snippet"):
                print(f"    Caller Code Snippet:\n{top.get('code_snippet')}")

        print("\n" + "=" * 70)
        print("  ALL AI REVERSE ENGINEERING & CRYPTO TESTS PASSED 100%!")
        print("=" * 70)

    finally:
        await controller.stop()
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(run_crypto_and_source_tests())
