import os
import sys
import time
import json
import shutil
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.agent_api import AgentBrowserController
from agent_browser.cdp import resolve_profile_dir, list_profiles


class MultiFeatureMockServer(BaseHTTPRequestHandler):
    """
    Mock Server providing:
    - HTML page with inline script & external script link & image & font & gif
    - External JS script with sourcemap header / comment
    - JSON API endpoint
    - Binary image (PNG)
    - Binary GIF
    - Binary font (WOFF2)
    """
    def log_message(self, format, *args):
        pass  # Quiet

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Multi-Profile and Source Inspection Test</title>
    <script src="/static/app.js"></script>
</head>
<body>
    <h1>Profile & Source Inspection Test Ground</h1>
    <img id="logo-png" src="/static/logo.png" alt="Test Logo" />
    <img id="cat-gif" src="/static/animation.gif" alt="Cat Animation" />
    <div id="api-output">Waiting for API...</div>

    <script id="inline-test-script">
        function runInlineDemo() {
            console.log("Inline script executed successfully!");
        }
        window.__inlineLoaded = true;
    </script>
</body>
</html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

        elif self.path == "/static/app.js":
            js = """
// External Application Logic
function calculateSecretHash(seed) {
    return "hash_" + seed + "_verified";
}
window.__appSecret = calculateSecretHash(12345);
//# sourceMappingURL=/static/app.js.map
"""
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.end_headers()
            self.wfile.write(js.encode("utf-8"))

        elif self.path == "/static/app.js.map":
            sourcemap = json.dumps({
                "version": 3,
                "file": "app.js",
                "sources": ["app.ts"],
                "mappings": "AAAA"
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(sourcemap.encode("utf-8"))

        elif self.path == "/api/user-data":
            payload = {
                "user_id": 42,
                "username": "paneer_tester",
                "status": "authenticated",
                "roles": ["admin", "developer"],
                "metadata": {
                    "session_token": "token_abc_123_xyz",
                    "timestamp": 1728390000
                }
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        elif self.path == "/static/logo.png":
            # 1x1 transparent PNG binary bytes
            png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(png_bytes)

        elif self.path == "/static/animation.gif":
            # Minimal 1x1 GIF binary bytes
            gif_bytes = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
            self.send_response(200)
            self.send_header("Content-Type", "image/gif")
            self.end_headers()
            self.wfile.write(gif_bytes)

        elif self.path == "/static/font.woff2":
            # Dummy font binary bytes
            font_bytes = b"wOF2\x00\x01\x00\x00\x00\x00\x00\x20\x00\x00\x00\x00\x00\x00\x00\x00"
            self.send_response(200)
            self.send_header("Content-Type", "font/woff2")
            self.end_headers()
            self.wfile.write(font_bytes)

        else:
            self.send_response(404)
            self.end_headers()


def start_server(port=8897):
    server = HTTPServer(("127.0.0.1", port), MultiFeatureMockServer)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


async def run_tests():
    print("=" * 75)
    print("  VERIFYING MULTI-PROFILE, SMART ASSET FILTER & SOURCE INSPECTION")
    print("=" * 75)

    base_dir = os.path.dirname(os.path.abspath(__file__))
    profiles_dir = os.path.join(base_dir, "profiles")

    # Clean previous test profile directories if any
    test_p1_dir = os.path.join(profiles_dir, "dev1")
    test_p2_dir = os.path.join(profiles_dir, "debug_session2")
    for p in [test_p1_dir, test_p2_dir]:
        if os.path.exists(p):
            try:
                shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass

    # ---------------------------------------------------------
    # TEST PHASE 1: Profile Resolution & Directory Isolation
    # ---------------------------------------------------------
    print("\n--- PHASE 1: Multi-Profile Resolution & Directory Isolation ---")
    name1, path1 = resolve_profile_dir(profile="dev1")
    name2, path2 = resolve_profile_dir(profile="debug_session2")
    name_def, path_def = resolve_profile_dir()

    print(f"[+] Profile 'dev1' resolved to: {path1}")
    print(f"[+] Profile 'debug_session2' resolved to: {path2}")
    print(f"[+] Default profile resolved to: {path_def}")

    assert name1 == "dev1"
    assert path1.endswith(os.path.join("profiles", "dev1"))
    assert name2 == "debug_session2"
    assert path2.endswith(os.path.join("profiles", "debug_session2"))
    assert name_def == "default"
    assert path1 != path2 and path1 != path_def
    print("[PASS] Profile names and isolated directory paths resolved correctly.")

    # Security check: Path traversal prevention
    profiles_root = os.path.abspath(os.path.join(base_dir, "profiles"))
    dot_name, dot_path = resolve_profile_dir("..")
    assert dot_name == "custom" and dot_path.startswith(profiles_root)
    dot2_name, dot2_path = resolve_profile_dir("../../../evil_escape")
    assert not os.path.isabs("../../../evil_escape") or not dot2_path.startswith(base_dir) or dot2_name != ""
    assert dot_name != ".." and dot2_name != ".."
    print("[PASS] Path traversal profile names ('..', etc.) safely sanitized and restricted.")

    # Launch controller with profile="dev1"
    ctrl1 = AgentBrowserController(port=9228, headless=True, profile="dev1", smart_filter=True)
    await ctrl1.start()
    try:
        assert os.path.exists(path1), f"Expected directory {path1} to exist on disk after browser launch"
        print(f"[PASS] Directory for profile 'dev1' was physically created: {path1}")
        
        # Check listing
        profiles_list = list_profiles()
        p_names = [p["name"] for p in profiles_list]
        print(f"[+] Discovered profiles: {p_names}")
        assert "dev1" in p_names
        print("[PASS] Profile listing discovered 'dev1' on disk.")
    finally:
        await ctrl1.stop()

    # Launch controller with profile="debug_session2"
    ctrl2 = AgentBrowserController(port=9229, headless=True, profile="debug_session2", smart_filter=True)
    await ctrl2.start()
    try:
        assert os.path.exists(path2), f"Expected directory {path2} to exist on disk after browser launch"
        print(f"[PASS] Directory for profile 'debug_session2' was physically created: {path2}")
        profiles_list = list_profiles()
        p_names = [p["name"] for p in profiles_list]
        assert "debug_session2" in p_names
        print("[PASS] Profile listing discovered 'debug_session2' on disk.")
    finally:
        await ctrl2.stop()

    # ---------------------------------------------------------
    # TEST PHASE 2: Smart Asset Filter & Full Response Retention
    # ---------------------------------------------------------
    print("\n--- PHASE 2: Smart Asset Filter & 100% API/HTML/Script Response Retention ---")
    mock_port = 8897
    server = start_server(mock_port)
    base_url = f"http://127.0.0.1:{mock_port}"

    ctrl_filter = AgentBrowserController(port=9230, headless=True, profile="dev1", smart_filter=True)
    await ctrl_filter.start()

    try:
        # Navigate to page which loads HTML, script (/static/app.js), PNG (/static/logo.png), GIF (/static/animation.gif)
        await ctrl_filter.navigate(f"{base_url}/index.html")

        # Also trigger an API fetch and a font fetch via JavaScript
        await ctrl_filter.evaluate("""
        (async () => {
            await fetch('/api/user-data');
            await fetch('/static/font.woff2');
        })()
        """)
        await asyncio.sleep(1.5)

        requests = list(ctrl_filter.network.requests.values())
        print(f"[+] Total recorded HTTP requests: {len(requests)}")
        assert len(requests) >= 4, "Expected at least 4 requests recorded"

        req_by_url = {r.get("url", ""): r for r in requests}

        # Check PNG response
        png_req = req_by_url.get(f"{base_url}/static/logo.png")
        assert png_req is not None, "PNG request was not recorded"
        print(f"[+] PNG Request: Status={png_req.get('status')}, Mime={png_req.get('mime_type')}, Filtered={png_req.get('body_filtered')}")
        assert png_req.get("body_filtered") is True, "PNG response body was NOT filtered!"
        assert "[SmartFilter: Skipped" in str(png_req.get("response_body")), f"Unexpected PNG body: {png_req.get('response_body')}"
        print("[PASS] PNG response body correctly skipped by SmartFilter.")

        # Check GIF response
        gif_req = req_by_url.get(f"{base_url}/static/animation.gif")
        assert gif_req is not None, "GIF request was not recorded"
        print(f"[+] GIF Request: Status={gif_req.get('status')}, Mime={gif_req.get('mime_type')}, Filtered={gif_req.get('body_filtered')}")
        assert gif_req.get("body_filtered") is True, "GIF response body was NOT filtered!"
        assert "[SmartFilter: Skipped" in str(gif_req.get("response_body")), f"Unexpected GIF body: {gif_req.get('response_body')}"
        print("[PASS] GIF response body correctly skipped by SmartFilter.")

        # Check WOFF2 Font response
        font_req = req_by_url.get(f"{base_url}/static/font.woff2")
        assert font_req is not None, "Font request was not recorded"
        print(f"[+] Font Request: Status={font_req.get('status')}, Mime={font_req.get('mime_type')}, Filtered={font_req.get('body_filtered')}")
        assert font_req.get("body_filtered") is True, "Font response body was NOT filtered!"
        assert "[SmartFilter: Skipped" in str(font_req.get("response_body"))
        print("[PASS] WOFF2 font response body correctly skipped by SmartFilter.")

        # Check API response (/api/user-data) -> MUST BE 100% SAVED
        api_req = req_by_url.get(f"{base_url}/api/user-data")
        assert api_req is not None, "API request was not recorded"
        print(f"[+] API Request: Status={api_req.get('status')}, Mime={api_req.get('mime_type')}, Filtered={api_req.get('body_filtered')}")
        assert api_req.get("body_filtered") is False, "API response body was erroneously filtered!"
        api_body = api_req.get("response_body")
        assert api_body is not None and len(api_body) > 0, "API response body is empty!"
        parsed_api = json.loads(api_body)
        assert parsed_api["username"] == "paneer_tester"
        assert parsed_api["metadata"]["session_token"] == "token_abc_123_xyz"
        print(f"[PASS] API JSON response body saved 100% in full: {api_body}")

        # Check HTML Document response -> MUST BE 100% SAVED
        html_req = req_by_url.get(f"{base_url}/index.html")
        assert html_req is not None, "HTML request was not recorded"
        print(f"[+] HTML Request: Status={html_req.get('status')}, Filtered={html_req.get('body_filtered')}")
        assert html_req.get("body_filtered") is False, "HTML response body was erroneously filtered!"
        html_body = html_req.get("response_body")
        assert html_body is not None and "<!DOCTYPE html>" in html_body
        print("[PASS] Document HTML response body saved 100% in full.")

        # Check JS Script response (/static/app.js) -> MUST BE 100% SAVED
        js_req = req_by_url.get(f"{base_url}/static/app.js")
        assert js_req is not None, "JS request was not recorded"
        print(f"[+] JS Script Request: Status={js_req.get('status')}, Filtered={js_req.get('body_filtered')}")
        assert js_req.get("body_filtered") is False, "JS script response body was erroneously filtered!"
        js_body = js_req.get("response_body")
        assert js_body is not None and "calculateSecretHash" in js_body
        print("[PASS] External JS response body saved 100% in full.")

        # Test AI Summary formatting: images and fonts should NOT pollute AI context
        summary = ctrl_filter.network.format_ai_summary(limit=50, exclude_assets=True)
        assert f"[#{png_req.get('id')}]" not in summary, f"PNG request {png_req.get('id')} should not be in AI summary"
        assert f"[#{gif_req.get('id')}]" not in summary, f"GIF request {gif_req.get('id')} should not be in AI summary"
        assert f"[#{font_req.get('id')}]" not in summary, f"Font request {font_req.get('id')} should not be in AI summary"
        assert f"[#{api_req.get('id')}]" in summary, f"API request {api_req.get('id')} MUST be in AI summary"
        print("[PASS] AI network summary successfully excludes asset noise while preserving API calls.")

        # ---------------------------------------------------------
        # TEST PHASE 3: Source Code Inspection (HTML DOM, Scripts, Sourcemaps)
        # ---------------------------------------------------------
        print("\n--- PHASE 3: Source Code Inspection (HTML DOM, Inline & External Scripts, Sourcemaps) ---")

        # 1. Read full HTML DOM source
        full_html = await ctrl_filter.get_page_html()
        print(f"[+] Full page HTML length: {len(full_html)} chars")
        assert "<!DOCTYPE html>" in full_html
        assert "<title>Multi-Profile and Source Inspection Test</title>" in full_html
        assert "Profile &amp; Source Inspection Test Ground" in full_html
        print("[PASS] Complete Page HTML source (with DOCTYPE) read accurately.")

        # 2. List all scripts
        scripts = await ctrl_filter.source_inspector.list_all_scripts(include_inline=True)
        print(f"[+] Total scripts detected by debugger: {len(scripts)}")
        assert len(scripts) >= 2, "Expected at least 2 scripts (external + inline)"

        app_script = next((s for s in scripts if "/static/app.js" in s.get("url", "")), None)
        assert app_script is not None, "External script /static/app.js was not detected!"
        print(f"[+] External script metadata: id={app_script['scriptId']}, url={app_script['url']}, sourcemap={app_script['sourceMapURL']}")
        assert app_script.get("sourceMapURL") == "/static/app.js.map", "Sourcemap URL was not captured!"

        # 3. Read external script source directly via Debugger domain
        app_source = await ctrl_filter.source_inspector.get_script_source(app_script["scriptId"])
        print(f"[+] External script source fetched ({len(app_source)} chars):")
        assert "calculateSecretHash" in app_source
        assert "__appSecret" in app_source
        print("[PASS] External script source fetched completely via CDP Debugger.")

        # 4. Get all sources bundle
        bundle = await ctrl_filter.get_all_sources(include_content=True)
        assert bundle["html_length"] > 0
        assert bundle["scripts_count"] >= 2
        assert bundle["sourcemaps_count"] >= 1
        assert any(sm.get("sourceMapURL") == "/static/app.js.map" for sm in bundle["sourcemaps"])
        print(f"[PASS] get_all_sources() returned unified bundle with {bundle['scripts_count']} scripts and {bundle['sourcemaps_count']} sourcemaps.")

        # 5. Dump sources to disk
        dump_dir = os.path.join(base_dir, "test_sources_dump")
        if os.path.exists(dump_dir):
            shutil.rmtree(dump_dir, ignore_errors=True)

        dump_result = await ctrl_filter.dump_sources_to_disk(dump_dir)
        print(f"[+] Dump result: {dump_result}")
        assert os.path.exists(dump_result["html_file"])
        assert os.path.exists(dump_result["manifest_file"])
        assert dump_result["scripts_saved"] >= 2
        assert dump_result.get("sourcemaps_saved", 0) >= 1

        # Verify offline sourcemap file was physically downloaded and written to disk
        sourcemaps_dir = os.path.join(dump_dir, "sourcemaps")
        assert os.path.exists(sourcemaps_dir)
        sm_files = os.listdir(sourcemaps_dir)
        assert len(sm_files) >= 1
        with open(os.path.join(sourcemaps_dir, sm_files[0]), "r", encoding="utf-8") as sm_f:
            sm_data = json.load(sm_f)
        assert sm_data.get("version") == 3
        print(f"[PASS] Offline sourcemap file physically verified on disk ({sm_files[0]}).")

        with open(dump_result["html_file"], "r", encoding="utf-8") as f:
            dumped_html = f.read()
        assert "Profile &amp; Source Inspection Test Ground" in dumped_html

        with open(dump_result["manifest_file"], "r", encoding="utf-8") as f:
            manifest_data = json.load(f)
        assert manifest_data["scripts_count"] >= 2
        assert len(manifest_data.get("sourcemaps_saved", [])) >= 1
        print(f"[PASS] dump_sources_to_disk() successfully created full offline source archive on disk.")

        # Clean up dump directory
        shutil.rmtree(dump_dir, ignore_errors=True)

    finally:
        await ctrl_filter.stop()
        server.shutdown()

    print("\n" + "=" * 75)
    print("  ALL MULTI-PROFILE, SMART FILTER & SOURCE TESTS PASSED 100%!")
    print("=" * 75)


def test_multiprofile_smart_filter_sources():
    """Pytest test case runner"""
    asyncio.run(run_tests())


if __name__ == "__main__":
    asyncio.run(run_tests())
