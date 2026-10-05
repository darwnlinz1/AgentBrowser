import os
import sys
import json
import argparse
import requests
import subprocess
import time
from typing import Optional, Dict, Any

DEFAULT_SERVER_URL = "http://127.0.0.1:9224"


def is_server_alive(server_url: str = DEFAULT_SERVER_URL) -> bool:
    try:
        r = requests.get(f"{server_url}/session/status", timeout=0.5)
        return r.status_code == 200
    except Exception:
        return False


def ensure_server(server_url: str = DEFAULT_SERVER_URL, headless: bool = False, browser_port: int = 9223):
    if not is_server_alive(server_url):
        # Spawn daemon in background
        cmd = [sys.executable, "-m", "agent_browser.server"]
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        )
        for _ in range(30):
            if is_server_alive(server_url):
                break
            time.sleep(0.2)

        if not is_server_alive(server_url):
            raise RuntimeError("Failed to start AgentBrowser background server.")

    # Auto start session if inactive
    try:
        status = requests.get(f"{server_url}/session/status", timeout=1.0).json()
        if not status.get("active"):
            requests.post(
                f"{server_url}/session/start",
                json={"headless": headless, "browser_port": browser_port},
                timeout=20.0
            )
    except Exception:
        pass


def make_request(method: str, path: str, data: Optional[Dict[str, Any]] = None, server_url: str = DEFAULT_SERVER_URL) -> Any:
    url = f"{server_url}{path}"
    if method == "GET":
        r = requests.get(url, params=data, timeout=30.0)
    else:
        r = requests.post(url, json=data or {}, timeout=30.0)

    if r.status_code >= 400:
        try:
            err = r.json()
            print(json.dumps({"error": err.get("detail", str(err))}, indent=2))
        except Exception:
            print(json.dumps({"error": r.text}, indent=2))
        sys.exit(1)

    return r.json()


def main():
    parser = argparse.ArgumentParser(description="AgentBrowser - Autonomous Agent Browser Debugging Platform CLI")
    parser.add_argument("--server", default=DEFAULT_SERVER_URL, help="Daemon server endpoint URL")
    parser.add_argument("--headless", action="store_true", help="Launch browser in headless mode")
    parser.add_argument("--port", type=int, default=9223, help="Browser CDP port")

    subparsers = parser.add_subparsers(dest="command")

    # start
    start_p = subparsers.add_parser("start", help="Start browser session and daemon")
    start_p.add_argument("--headless", action="store_true", help="Launch browser in headless mode")
    start_p.add_argument("--port", type=int, default=9223, help="Browser CDP port")
    start_p.add_argument("--server", default=DEFAULT_SERVER_URL, help="Daemon server endpoint URL")

    # stop
    subparsers.add_parser("stop", help="Stop browser session")

    # status
    subparsers.add_parser("status", help="Get session status")

    # open / navigate
    nav_p = subparsers.add_parser("open", help="Navigate to URL")
    nav_p.add_argument("url", help="Target URL")

    # view
    subparsers.add_parser("view", help="Print token-efficient agent-readable page view")

    # snapshot
    subparsers.add_parser("snapshot", help="Dump interactive semantic DOM snapshot")

    # click
    click_p = subparsers.add_parser("click", help="Click an element and trace causal effects")
    click_p.add_argument("selector", help="CSS selector or #ref number (e.g. #3 or button.submit)")

    # type
    type_p = subparsers.add_parser("type", help="Type text into an input element and trace causal effects")
    type_p.add_argument("selector", help="CSS selector or #ref number")
    type_p.add_argument("text", help="Text to type")
    type_p.add_argument("--no-clear", action="store_true", help="Do not clear existing value")

    # trace
    trace_p = subparsers.add_parser("trace", help="Execute an action with full causal event tracing")
    trace_p.add_argument("action", choices=["click", "type", "navigate", "eval", "hover", "scroll"])
    trace_p.add_argument("--target", default=None, help="Target selector or parameter")
    trace_p.add_argument("--params", default="{}", help="JSON params object")
    trace_p.add_argument("--settle", type=float, default=1.0, help="Settle timeout in seconds")

    # eval
    eval_p = subparsers.add_parser("eval", help="Execute JavaScript in active page")
    eval_p.add_argument("expression", help="JavaScript code to evaluate")

    # traffic
    net_p = subparsers.add_parser("traffic", help="Search and view recorded network traffic")
    net_p.add_argument("--search", default=None, help="Text search in URL/body/headers")
    net_p.add_argument("--status", type=int, default=None, help="Filter by HTTP status code")
    net_p.add_argument("--mime", default=None, help="Filter by MIME type")
    net_p.add_argument("--limit", type=int, default=20, help="Max results")

    # replay
    rep_p = subparsers.add_parser("replay", help="Replay a captured request and compute diff against baseline")
    rep_p.add_argument("request_id", help="RequestId of request to replay")
    rep_p.add_argument("--mutate-header", action="append", help="Header to mutate in Name=Value format")
    rep_p.add_argument("--mutate-body", default=None, help="Replacement request body")
    rep_p.add_argument("--mutate-url", default=None, help="Replacement request URL")

    # intercept
    int_p = subparsers.add_parser("intercept", help="Manage network interception rules")
    int_sub = int_p.add_subparsers(dest="intercept_cmd")

    int_add = int_sub.add_parser("add", help="Add interception rule")
    int_add.add_argument("url_pattern", help="URL regex pattern or glob")
    int_add.add_argument("--rule-id", default=None, help="Unique rule ID")
    int_add.add_argument("--action", choices=["fulfill", "fail", "modify_request"], default="fulfill")
    int_add.add_argument("--mock-status", type=int, default=200)
    int_add.add_argument("--mock-body", default="{}", help="Mock response body")
    int_add.add_argument("--fail-reason", default="Failed")

    int_sub.add_parser("clear", help="Clear all interception rules")

    # diagnose
    subparsers.add_parser("diagnose", help="Run automated root-cause diagnostics on current page")

    # cookies
    ck_p = subparsers.add_parser("cookies", help="Inspect or set cookies")
    ck_p.add_argument("--set", default=None, help="Set cookie format: name=value[;domain=...][;path=...]")

    # storage
    st_p = subparsers.add_parser("storage", help="Inspect or set LocalStorage/SessionStorage")
    st_p.add_argument("--type", choices=["local", "session"], default="local")
    st_p.add_argument("--set-key", default=None, help="Storage key")
    st_p.add_argument("--set-val", default=None, help="Storage value")

    # screenshot
    ss_p = subparsers.add_parser("screenshot", help="Capture page screenshot")
    ss_p.add_argument("--output", default="screenshot.png", help="Output file path")
    ss_p.add_argument("--full-page", action="store_true", help="Capture full scrollable page")

    # mutate-dom
    dom_mut = subparsers.add_parser("mutate-dom", help="Directly mutate DOM node")
    dom_mut.add_argument("selector", help="Selector or #ref")
    dom_mut.add_argument("action", choices=["set_attribute", "remove_attribute", "set_html", "set_text", "set_style", "remove_node"])
    dom_mut.add_argument("--kwargs", default="{}", help="JSON arguments for mutation")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "start":
        ensure_server(args.server, headless=args.headless, browser_port=args.port)
        print(json.dumps({"status": "running", "server": args.server, "browser_port": args.port}))
        return

    if args.command == "stop":
        if is_server_alive(args.server):
            res = make_request("POST", "/session/stop", server_url=args.server)
            print(json.dumps(res, indent=2))
        else:
            print(json.dumps({"status": "already_stopped"}))
        return

    # For all other commands, ensure server is up
    ensure_server(args.server, headless=args.headless, browser_port=args.port)

    if args.command == "status":
        res = make_request("GET", "/session/status", server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "open":
        res = make_request("POST", "/navigate", {"url": args.url}, server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "view":
        res = make_request("GET", "/agent_view", server_url=args.server)
        print(res.get("view", ""))

    elif args.command == "snapshot":
        res = make_request("GET", "/snapshot", server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "click":
        res = make_request(
            "POST",
            "/trace",
            {"action": "click", "target": args.selector, "settle_timeout": 1.0},
            server_url=args.server
        )
        print(json.dumps(res, indent=2))

    elif args.command == "type":
        res = make_request(
            "POST",
            "/trace",
            {
                "action": "type",
                "target": args.selector,
                "params": {"text": args.text, "clear_first": not args.no_clear},
                "settle_timeout": 1.0
            },
            server_url=args.server
        )
        print(json.dumps(res, indent=2))

    elif args.command == "trace":
        try:
            p_dict = json.loads(args.params)
        except Exception:
            p_dict = {}
        res = make_request(
            "POST",
            "/trace",
            {
                "action": args.action,
                "target": args.target,
                "params": p_dict,
                "settle_timeout": args.settle
            },
            server_url=args.server
        )
        print(json.dumps(res, indent=2))

    elif args.command == "eval":
        res = make_request("POST", "/eval", {"expression": args.expression}, server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "traffic":
        params: Dict[str, Any] = {"limit": args.limit}
        if args.search:
            params["query"] = args.search
        if args.status:
            params["status"] = args.status
        if args.mime:
            params["mime"] = args.mime
        res = make_request("GET", "/traffic", params, server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "replay":
        mut_headers = {}
        if args.mutate_header:
            for mh in args.mutate_header:
                if "=" in mh:
                    k, v = mh.split("=", 1)
                    mut_headers[k.strip()] = v.strip()
        payload = {
            "requestId": args.request_id,
            "mutate_headers": mut_headers or None,
            "mutate_body": args.mutate_body,
            "mutate_url": args.mutate_url
        }
        res = make_request("POST", "/replay", payload, server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "intercept":
        if args.intercept_cmd == "add":
            rid = args.rule_id or f"rule_{int(time.time()*1000)}"
            rule_payload = {
                "rule_id": rid,
                "url_pattern": args.url_pattern,
                "action": args.action,
                "mock_status": args.mock_status,
                "mock_body": args.mock_body,
                "fail_reason": args.fail_reason
            }
            res = make_request("POST", "/intercept/add", rule_payload, server_url=args.server)
            print(json.dumps(res, indent=2))
        elif args.intercept_cmd == "clear":
            res = make_request("POST", "/intercept/clear", {}, server_url=args.server)
            print(json.dumps(res, indent=2))
        else:
            print("Usage: intercept add|clear ...")

    elif args.command == "diagnose":
        res = make_request("GET", "/diagnostics", server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "cookies":
        if args.set:
            # parse cookie string
            parts = args.set.split(";")
            nv = parts[0].split("=", 1)
            name = nv[0].strip()
            val = nv[1].strip() if len(nv) > 1 else ""
            c_dict: Dict[str, Any] = {"name": name, "value": val}
            for p in parts[1:]:
                if "=" in p:
                    pk, pv = p.split("=", 1)
                    pk = pk.strip().lower()
                    pv = pv.strip()
                    if pk == "domain":
                        c_dict["domain"] = pv
                    elif pk == "path":
                        c_dict["path"] = pv
            res = make_request("POST", "/cookies", c_dict, server_url=args.server)
            print(json.dumps(res, indent=2))
        else:
            res = make_request("GET", "/cookies", server_url=args.server)
            print(json.dumps(res, indent=2))

    elif args.command == "storage":
        if args.set_key and args.set_val is not None:
            res = make_request(
                "POST",
                "/storage",
                {"storage_type": args.type, "key": args.set_key, "value": args.set_val},
                server_url=args.server
            )
            print(json.dumps(res, indent=2))
        else:
            res = make_request("GET", "/storage", {"storage_type": args.type}, server_url=args.server)
            print(json.dumps(res, indent=2))

    elif args.command == "screenshot":
        res = make_request(
            "POST",
            "/screenshot",
            {"path": args.output, "full_page": args.full_page},
            server_url=args.server
        )
        print(json.dumps(res, indent=2))

    elif args.command == "mutate-dom":
        try:
            kw = json.loads(args.kwargs)
        except Exception:
            kw = {}
        res = make_request(
            "POST",
            "/mutate_dom",
            {"selector": args.selector, "action": args.action, "kwargs": kw},
            server_url=args.server
        )
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
