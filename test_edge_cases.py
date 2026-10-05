import os
import sys
import json
import asyncio

# Ensure agent_browser is importable
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.agent_api import AgentBrowserController
from agent_browser.network import InterceptRule

async def test_edge_cases():
    print("=" * 60)
    print("  RUNNING AGENTBROWSER EDGE CASE & RECOVERY SUITE")
    print("=" * 60)

    controller = AgentBrowserController(port=9227, headless=True)
    await controller.start()

    try:
        # Edge Case 1: Bad JS Syntax Evaluation
        print("\n--- Edge Case 1: Malformed JS Syntax Handling ---")
        res = await controller.evaluate("const foo = ;")
        print(f"[+] Syntax error evaluation result: {res}")
        assert res["success"] is False
        assert "error" in res or "exception" in res
        print("[PASS] Malformed JS syntax caught gracefully.")

        # Edge Case 2: Click Non-Existent Selector
        print("\n--- Edge Case 2: Non-Existent Element Click Tracing ---")
        trace = await controller.trace_action("click", target="#non-existent-button-999")
        print(f"[+] Action Trace: success={trace['action']['success']}, error={trace['action']['error']}")
        assert trace["action"]["success"] is False
        assert "not found" in trace["action"]["error"].lower()
        print("[PASS] Non-existent element error trapped in Action Trace Report.")

        # Edge Case 3: Replay Request Not Found
        print("\n--- Edge Case 3: Replay Unknown Request ID ---")
        try:
            controller.network.replay_request("invalid-req-id-123")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            print(f"[+] ValueError correctly raised: {e}")
            print("[PASS] Unknown request replay rejected.")

        # Edge Case 4: Storage operations on about:blank
        print("\n--- Edge Case 4: LocalStorage Safety on about:blank ---")
        store = await controller.storage.get_storage("local")
        print(f"[+] Storage response: {store}")
        print("[PASS] LocalStorage evaluated safely.")

        print("\n" + "=" * 60)
        print("  ALL EDGE CASES AND FAILURE MODES HANDLED SAFELY!")
        print("=" * 60)

    finally:
        await controller.stop()

if __name__ == "__main__":
    asyncio.run(test_edge_cases())
