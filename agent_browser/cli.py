import os
import sys
import json
import argparse
import requests
import subprocess
import time
from typing import Optional, Dict, Any

from agent_browser import security

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DEFAULT_SERVER_URL = "http://127.0.0.1:9224"


def is_server_alive(server_url: str = DEFAULT_SERVER_URL) -> bool:
    try:
        r = requests.get(f"{server_url}/session/status", timeout=0.5)
        return r.status_code == 200
    except Exception:
        return False


def _auth_headers(server_url: str = DEFAULT_SERVER_URL) -> Dict[str, str]:
    """Attach the loopback daemon bearer token so hardened routes accept us."""
    from urllib.parse import urlparse
    port = urlparse(server_url).port or 9224
    tok = security.read_token(port)
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _spawn_daemon(server_url: str):
    """Start this tree's daemon bound to the host and port in server_url."""
    from urllib.parse import urlparse
    parsed = urlparse(server_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 9224
    cmd = [sys.executable, "-m", "agent_browser.server", "--host", host, "--port", str(port)]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = 0x00000008 | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )


def ensure_server(
    server_url: str = DEFAULT_SERVER_URL,
    headless: bool = False,
    browser_port: int = 9223,
    profile: Optional[str] = None,
    profile_dir: Optional[str] = None,
    smart_filter: bool = True,
    auto_start_session: bool = True
):
    if not is_server_alive(server_url):
        _spawn_daemon(server_url)
        for _ in range(40):
            if is_server_alive(server_url):
                break
            time.sleep(0.15)

        if not is_server_alive(server_url):
            raise RuntimeError("Failed to start AgentBrowser background server.")

    if not auto_start_session:
        return

    # Auto start session if inactive or switch profile if requested
    try:
        from agent_browser.cdp import resolve_profile_dir
        req_name, req_dir = resolve_profile_dir(profile=profile, profile_dir=profile_dir)
        status = requests.get(f"{server_url}/session/status", timeout=1.0).json()

        if not status.get("active"):
            requests.post(
                f"{server_url}/session/start",
                json={
                    "headless": headless,
                    "browser_port": browser_port,
                    "profile": profile,
                    "profile_dir": profile_dir,
                    "smart_filter": smart_filter
                },
                timeout=25.0,
                headers=_auth_headers(server_url)
            )
        elif profile is not None or profile_dir is not None:
            # Active session running a different profile -> switch
            if status.get("profile_name") != req_name or status.get("profile_dir") != req_dir:
                requests.post(
                    f"{server_url}/session/start",
                    json={
                        "headless": headless,
                        "browser_port": browser_port,
                        "profile": profile,
                        "profile_dir": profile_dir,
                        "smart_filter": smart_filter
                    },
                    timeout=25.0,
                    headers=_auth_headers(server_url)
                )
    except Exception:
        pass


def make_request(method: str, path: str, data: Optional[Dict[str, Any]] = None, server_url: str = DEFAULT_SERVER_URL) -> Any:
    url = f"{server_url}{path}"
    headers = _auth_headers(server_url)
    if method == "GET":
        r = requests.get(url, params=data, timeout=30.0, headers=headers)
    else:
        r = requests.post(url, json=data or {}, timeout=30.0, headers=headers)

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
    parser.add_argument("--server", default=None, help="Daemon server endpoint URL")
    parser.add_argument("--headless", action="store_true", help="Launch browser in headless mode")
    parser.add_argument("--port", type=int, default=None, help="Browser CDP port")
    parser.add_argument("--profile", default=None, help="Browser profile name or directory (e.g. dev1, debug_session2)")
    parser.add_argument("--no-smart-filter", action="store_true", help="Disable smart asset filter")

    subparsers = parser.add_subparsers(dest="command")

    # start
    start_p = subparsers.add_parser("start", help="Start browser session and daemon")
    # Defaults stay None so a parent --server/--port/--profile/--headless is not
    # overwritten by the subparser's own default.
    # SUPPRESS defaults: a value given *before* the subcommand (parent parser)
    # must not be overwritten by the subparser's own default.
    start_p.add_argument("--headless", action="store_true", default=argparse.SUPPRESS, help="Launch browser in headless mode")
    start_p.add_argument("--port", type=int, default=argparse.SUPPRESS, help="Browser CDP port")
    start_p.add_argument("--server", default=argparse.SUPPRESS, help="Daemon server endpoint URL")
    start_p.add_argument("--profile", default=argparse.SUPPRESS, help="Browser profile name or directory (e.g. dev1, debug_session2)")
    start_p.add_argument("--no-smart-filter", action="store_true", default=argparse.SUPPRESS, help="Disable smart asset filter")

    # profiles
    subparsers.add_parser("profiles", help="List all local browser profiles")

    # tabs / activate-tab (page targets attached to the running browser)
    subparsers.add_parser("tabs", help="List attached page tabs")
    at_p = subparsers.add_parser("activate-tab", help="Make a page tab the active session")
    at_p.add_argument("session_id", help="Target session id from 'tabs'")

    # page-source / source-html
    ps_p = subparsers.add_parser("page-source", help="Get complete HTML source of active page")
    ps_p.add_argument("--output", default=None, help="Optional file path to save HTML")

    # dump-sources
    ds_p = subparsers.add_parser("dump-sources", help="Dump all page sources (HTML, scripts, sourcemaps)")
    ds_p.add_argument("--output-dir", default=None, help="Optional directory to save full sources bundle")
    ds_p.add_argument("--no-content", action="store_true", help="Omit script source bodies from returned JSON")

    # stop
    stop_p = subparsers.add_parser("stop", help="Stop browser session and the daemon bound to --server")
    stop_p.add_argument("--shutdown-daemon", action="store_true", default=True, help="Also stop the daemon process")

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
    rep_p.add_argument("--allow-write", action="store_true", help="Allow replaying POST/PUT/PATCH/DELETE (explicit opt-in)")

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
    subparsers.add_parser("perf", help="Web-vitals snapshot (LCP/CLS/long-tasks) + renderer metrics")
    subparsers.add_parser("dialogs", help="List JavaScript dialogs seen + current policy")
    dp_p = subparsers.add_parser("dialog-policy", help="Set JavaScript dialog policy")
    dp_p.add_argument("policy", choices=["accept", "dismiss"])
    subparsers.add_parser("downloads", help="List downloads captured in this session")
    ho_p = subparsers.add_parser("handoff", help="Human handoff: pause agent, hand browser to a human")
    ho_sub = ho_p.add_subparsers(dest="handoff_cmd")
    ho_start = ho_sub.add_parser("start", help="Start handoff (agent actions are refused until complete)")
    ho_start.add_argument("reason", nargs="?", default="")
    ho_sub.add_parser("status", help="Handoff status")
    ho_done = ho_sub.add_parser("complete", help="Complete handoff and resume agent control")
    ho_done.add_argument("summary", nargs="?", default="")

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

    # user-events
    ue_p = subparsers.add_parser("user-events", help="View real-time captured human user interactions")
    ue_p.add_argument("--limit", type=int, default=20, help="Max events to return")
    ue_p.add_argument("--type", default=None, help="Filter event type: click, change, scroll")

    # last-action
    subparsers.add_parser("last-action", help="Get the most recent human action on page")

    # pin
    pin_p = subparsers.add_parser("pin", help="Pin a specific DOM selector/region to watch")
    pin_p.add_argument("selector", help="CSS selector of element to pin")
    pin_p.add_argument("--alias", default=None, help="Optional short alias name")

    # unpin
    unpin_p = subparsers.add_parser("unpin", help="Unpin a previously watched region")
    unpin_p.add_argument("target", help="Alias or selector to unpin")

    # focus-view
    subparsers.add_parser("focus-view", help="View content and deltas of pinned regions only")

    # watch
    subparsers.add_parser("watch", help="Directed debugging dashboard: focused regions + last action")

    # ai-traffic
    ai_traf = subparsers.add_parser("ai-traffic", help="View token-efficient network summary optimized for LLM")
    ai_traf.add_argument("--limit", type=int, default=25, help="Max requests to list")

    # scripts
    scr_p = subparsers.add_parser("scripts", help="List all scripts loaded on the page")
    scr_p.add_argument("--no-inline", action="store_true", help="Exclude inline script tags")

    # script-source
    subparsers.add_parser("reconstruct", help="Rebuild original sources from source maps + endpoint inventory")
    scrs_p = subparsers.add_parser("script-source", help="Get full source code of script by ID")
    scrs_p.add_argument("script_id", help="Script ID from 'scripts' command")

    # find-crypto
    fc_p = subparsers.add_parser("find-crypto", help="Search scripts for crypto/API/encryption functions")
    fc_p.add_argument("--script-id", default=None, help="Specific script ID to analyze")

    # crypto-logs
    subparsers.add_parser("crypto-logs", help="Get pre-encryption plaintext logs intercepted by early hook")

    # caller
    cal_p = subparsers.add_parser("caller", help="Find source code caller that initiated an HTTP request")
    cal_p.add_argument("request_id", help="Request ID from network list")

    # proxy-traffic
    pt_p = subparsers.add_parser("proxy-traffic", help="Get traffic captured by embedded MITM proxy")
    pt_p.add_argument("--query", default=None, help="Filter by text or URL")
    pt_p.add_argument("--limit", type=int, default=50, help="Max entries to return")

    # mcp (tool catalog; the stdio server itself is agent_browser.mcp_server)
    mcp_p = subparsers.add_parser("mcp", help="MCP server tools for agent clients")
    mcp_p.add_argument("--list-tools", action="store_true", help="Print the MCP tool catalog as JSON")

    args = parser.parse_args()
    if getattr(args, "server", None) is None:
        args.server = DEFAULT_SERVER_URL
    if getattr(args, "port", None) is None:
        args.port = 9223
    if getattr(args, "headless", None) is None:
        args.headless = False
    if getattr(args, "no_smart_filter", None) is None:
        args.no_smart_filter = False
    if not hasattr(args, "profile"):
        args.profile = None

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "mcp":
        from agent_browser.mcp_server import list_tools
        if getattr(args, "list_tools", False):
            print(json.dumps(list_tools(), indent=2))
        else:
            print("Run the stdio MCP server with: python -m agent_browser.mcp_server  (requires 'agent-browser[mcp]')")
        return

    if args.command == "start":
        smart_filter = not getattr(args, "no_smart_filter", False)
        ensure_server(args.server, headless=args.headless, browser_port=args.port, profile=args.profile, smart_filter=smart_filter, auto_start_session=False)
        res = make_request("POST", "/session/start", {
            "headless": args.headless,
            "browser_port": args.port,
            "profile": args.profile,
            "smart_filter": smart_filter
        }, server_url=args.server)
        print(json.dumps(res, indent=2))
        return

    if args.command == "stop":
        if is_server_alive(args.server):
            try:
                session = make_request("POST", "/session/stop", server_url=args.server)
            except SystemExit:
                session = {"status": "stop_failed"}
            daemon = {"status": "left_running"}
            if getattr(args, "shutdown_daemon", True):
                try:
                    r = requests.post(f"{args.server}/shutdown", json={}, timeout=3.0, headers=_auth_headers(args.server))
                    daemon = r.json() if r.content else {"status": "shutdown_requested"}
                except Exception:
                    daemon = {"status": "shutdown_requested"}
            print(json.dumps({"session": session, "daemon": daemon}, indent=2))
        else:
            print(json.dumps({"status": "already_stopped"}))
        return

    # For all other commands, ensure server is up
    smart_filter = not getattr(args, "no_smart_filter", False)
    ensure_server(args.server, headless=args.headless, browser_port=args.port, profile=args.profile, smart_filter=smart_filter)

    if args.command == "status":
        res = make_request("GET", "/session/status", server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "profiles":
        res = make_request("GET", "/profiles", server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "tabs":
        res = make_request("GET", "/targets", server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "activate-tab":
        res = make_request("POST", "/targets/activate", {"session_id": args.session_id}, server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "page-source":
        res = make_request("GET", "/source/html", server_url=args.server)
        html = res.get("html", "")
        if args.output:
            with open(args.output, "w", encoding="utf-8", errors="replace") as f:
                f.write(html)
            print(json.dumps({"status": "saved", "file": args.output, "length": len(html)}))
        else:
            print(html)

    elif args.command == "dump-sources":
        if args.output_dir:
            res = make_request("POST", "/source/dump_to_disk", {"output_dir": args.output_dir}, server_url=args.server)
            print(json.dumps(res, indent=2))
        else:
            res = make_request("GET", "/source/dump", {"include_content": not args.no_content}, server_url=args.server)
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
            "mutate_url": args.mutate_url,
            "allow_write": bool(getattr(args, "allow_write", False))
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

    elif args.command == "perf":
        res = make_request("GET", "/perf/vitals", server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "dialogs":
        res = make_request("GET", "/dialogs", server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "dialog-policy":
        res = make_request("POST", "/dialog/policy", {"policy": args.policy}, server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "downloads":
        res = make_request("GET", "/downloads", server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "handoff":
        if args.handoff_cmd == "start":
            res = make_request("POST", "/handoff/start", {"reason": args.reason}, server_url=args.server)
        elif args.handoff_cmd == "complete":
            res = make_request("POST", "/handoff/complete", {"summary": args.summary}, server_url=args.server)
        else:
            res = make_request("GET", "/handoff", server_url=args.server)
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

    elif args.command == "user-events":
        params = {"limit": args.limit}
        if args.type:
            params["event_type"] = args.type
        res = make_request("GET", "/user/actions", params, server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "last-action":
        res = make_request("GET", "/user/last_action", server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "pin":
        res = make_request("POST", "/focus/pin", {"selector": args.selector, "alias": args.alias}, server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "unpin":
        res = make_request("POST", "/focus/unpin", {"target": args.target}, server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "focus-view":
        res = make_request("GET", "/focus/view", server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "watch":
        last_act = make_request("GET", "/user/last_action", server_url=args.server)
        focus_view = make_request("GET", "/focus/view", server_url=args.server)
        dashboard = {
            "last_user_action": last_act.get("last_action"),
            "focused_regions": focus_view
        }
        print(json.dumps(dashboard, indent=2))

    elif args.command == "ai-traffic":
        res = make_request("GET", "/network/ai_summary", {"limit": args.limit}, server_url=args.server)
        print(res.get("summary", ""))

    elif args.command == "scripts":
        res = make_request("GET", "/source/scripts", {"include_inline": not args.no_inline}, server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "reconstruct":
        res = make_request("GET", "/source/reconstruct", server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "script-source":
        res = make_request("GET", "/source/code", {"script_id": args.script_id}, server_url=args.server)
        print(res.get("source", ""))

    elif args.command == "find-crypto":
        params = {}
        if args.script_id:
            params["script_id"] = args.script_id
        res = make_request("GET", "/source/find_crypto", params, server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "crypto-logs":
        # Local operator explicitly opting in to the sensitive plaintext view.
        res = make_request("GET", "/source/crypto_logs", {"include_secrets": True}, server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "caller":
        res = make_request("GET", "/source/caller_for_request", {"request_id": args.request_id}, server_url=args.server)
        print(json.dumps(res, indent=2))

    elif args.command == "proxy-traffic":
        params = {"limit": args.limit}
        if args.query:
            params["query"] = args.query
        res = make_request("GET", "/proxy/traffic", params, server_url=args.server)
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
