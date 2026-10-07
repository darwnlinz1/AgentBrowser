import os
import sys
import time
import json
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.agent_api import AgentBrowserController


class InteractiveTestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        html = """<!DOCTYPE html>
<html>
<head>
    <title>User Tracking & Focus Test</title>
    <style>
        body { margin: 20px; font-family: sans-serif; }
        #watch-box { border: 2px solid blue; padding: 15px; margin: 20px 0; background: #f0f4ff; }
        #cart-status { font-weight: bold; color: green; }
        button { padding: 10px 20px; cursor: pointer; }
    </style>
</head>
<body>
    <h1>User Tracking Test Page</h1>
    
    <div id="watch-box">
        <h3>Pinned Region: Cart Summary</h3>
        <p id="cart-status">Items in Cart: 0</p>
        <input id="item-input" type="text" placeholder="Item name" value="GPU RTX" />
        <button id="btn-add" onclick="addItem()">Add to Cart</button>
    </div>

    <button id="btn-outside" onclick="console.log('Clicked outside')">Outside Button</button>

    <script>
        let count = 0;
        function addItem() {
            count++;
            document.getElementById('cart-status').textContent = 'Items in Cart: ' + count;
        }
    </script>
</body>
</html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))


def start_test_server(port=8896):
    server = HTTPServer(("127.0.0.1", port), InteractiveTestHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


async def run_user_tracking_tests():
    print("=" * 70)
    print("  RUNNING USER TRACKING & FOCUS PINNING VERIFICATION SUITE")
    print("=" * 70)

    server = start_test_server(8896)
    base_url = "http://127.0.0.1:8896"

    controller = AgentBrowserController(port=9240, headless=True)
    await controller.start()

    try:
        # Step 1: Navigate to page
        print(f"[*] Navigating to {base_url}...")
        await controller.navigate(base_url)

        # Step 2: Pin specific region (#watch-box)
        print("\n--- PHASE 1: Pinning Focus Region ---")
        pin_name = controller.focus.pin("#watch-box", alias="cart_summary")
        print(f"[+] Pinned selector '#watch-box' as alias '{pin_name}'")
        
        initial_focus = await controller.focus.get_focused_view()
        print(f"[+] Initial Focused View:")
        print(f"    Region found: {initial_focus['regions']['cart_summary']['found']}")
        print(f"    Text: {initial_focus['regions']['cart_summary']['text']}")
        assert initial_focus['regions']['cart_summary']['found'] is True
        assert "Items in Cart: 0" in initial_focus['regions']['cart_summary']['text']

        # Step 3: Trigger physical user interaction (Click Add button)
        print("\n--- PHASE 2: User Action Capture (Pixel & Selector) ---")
        await controller.dom.click("#btn-add")
        # Give a small moment for CDP binding to receive event
        await asyncio.sleep(0.5)

        actions = controller.user_tracker.get_actions(event_type="click")
        print(f"[+] Captured user click events count: {len(actions)}")
        assert len(actions) >= 1, "User click action not recorded by tracker"
        
        last_action = controller.user_tracker.get_last_action()
        print(f"[+] Last Captured Action Details:")
        print(f"    Event type: {last_action['type']}")
        print(f"    Pixel coords: x={last_action['pixel']['x']}, y={last_action['pixel']['y']}")
        print(f"    Target tag: {last_action['element']['tag']}")
        print(f"    Target selector: {last_action['element']['selector']}")
        print(f"    Target text: {last_action['element']['text']}")
        
        assert last_action['element']['tag'] == 'button'
        assert '#btn-add' in last_action['element']['selector']
        assert last_action['pixel']['x'] > 0 and last_action['pixel']['y'] > 0

        # Step 4: Verify Focused View Delta
        print("\n--- PHASE 3: Subtree Delta on Pinned Region ---")
        updated_focus = await controller.focus.get_focused_view()
        print(f"[+] Updated Focused View:")
        print(f"    New Text: {updated_focus['regions']['cart_summary']['text']}")
        print(f"    Delta changed: {updated_focus['deltas']['cart_summary']['changed']}")
        
        assert "Items in Cart: 1" in updated_focus['regions']['cart_summary']['text']
        assert updated_focus['deltas']['cart_summary']['changed'] is True, "Delta change not detected"

        print("\n" + "=" * 70)
        print("  ALL USER TRACKING & FOCUS TESTS PASSED WITH 100% SUCCESS!")
        print("=" * 70)

    finally:
        await controller.stop()
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(run_user_tracking_tests())
