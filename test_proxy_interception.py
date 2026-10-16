import time
import requests
from agent_browser.proxy_engine import LocalProxyServer


def test_proxy_interception():
    print("=" * 60)
    print("  TESTING EMBEDDED LOCAL MITM PROXY ENGINE")
    print("=" * 60)

    proxy_port = 8893
    server = LocalProxyServer(host="127.0.0.1", port=proxy_port)
    server.start()

    try:
        assert server.is_running() is True, "Proxy server failed to start"
        print(f"[+] Proxy successfully listening on 127.0.0.1:{proxy_port}")

        # Send test request through proxy
        proxies = {
            "http": f"http://127.0.0.1:{proxy_port}",
            "https": f"http://127.0.0.1:{proxy_port}"
        }

        print("[*] Sending test HTTP request through local proxy...")
        test_url = "http://example.com"
        r = requests.get(test_url, proxies=proxies, timeout=10)
        print(f"[+] Request status: {r.status_code}")

        # Check recorded traffic in hub
        flows = server.hub.get_flows(limit=10)
        print(f"[+] Flows recorded in Hub: {len(flows)}")
        assert len(flows) >= 1, "Flow was not recorded by ProxyTrafficHub"

        last_flow = flows[0]
        print(f"    Recorded URL: {last_flow['url']}")
        print(f"    Method: {last_flow['method']}")
        print(f"    Status: {last_flow['status']}")
        print(f"    Response body length: {len(last_flow.get('response_body') or '')}")

        assert "example.com" in last_flow["url"]
        assert last_flow["status"] == 200

        print("\n" + "=" * 60)
        print("  EMBEDDED PROXY VERIFICATION PASSED WITH 100% SUCCESS!")
        print("=" * 60)

    finally:
        server.stop()


if __name__ == "__main__":
    test_proxy_interception()
