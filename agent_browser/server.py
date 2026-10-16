import os
import sys
import asyncio
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

from agent_browser.agent_api import AgentBrowserController
from agent_browser.network import InterceptRule
from agent_browser.cdp import list_profiles
from agent_browser.evidence import redact_entry
from agent_browser import security
from agent_browser.session_registry import SessionRegistry, find_free_port

app = FastAPI(title="AgentBrowser Debugger API", version="1.0.0")

controller: Optional[AgentBrowserController] = None

# Multi-session registry. ``controller`` above remains the default-session
# alias for backward compatibility with existing routes and tests.
SESSIONS = SessionRegistry()

# Per-daemon loopback bearer token, set in run_server(). None => auth disabled
# (e.g. when the app object is imported directly in a unit test).
DAEMON_TOKEN: Optional[str] = None

# File-writing routes are confined to this root so a caller cannot write
# anywhere on disk.
_OUTPUT_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent_output")

# Routes reachable without the bearer token (health probe the CLI hits before
# it has read the token file).
_OPEN_PATHS = {"/session/status"}

# In-page code execution (eval / DOM mutation) can be disabled for locked-down
# deployments. Enabled by default so interactive debugging keeps working.
_EVAL_DISABLED = os.environ.get("AGENTBROWSER_DISABLE_EVAL", "").strip().lower() in ("1", "true", "yes", "on")

_SECRET_STORAGE_TOKENS = (
    "token", "secret", "password", "passwd", "auth", "jwt",
    "session", "api_key", "apikey", "credential",
)


def _looks_secret(key: str) -> bool:
    k = str(key).lower()
    return any(tok in k for tok in _SECRET_STORAGE_TOKENS)


@app.middleware("http")
async def _loopback_guard(request: Request, call_next):
    """Host/Origin validation (DNS-rebinding + cross-site defense) + bearer auth."""
    if not security.host_allowed(request.headers.get("host")):
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    if not security.origin_allowed(request.headers.get("origin")):
        return JSONResponse({"detail": "forbidden origin"}, status_code=403)
    if request.url.path not in _OPEN_PATHS and not security.bearer_ok(
        request.headers.get("authorization"), DAEMON_TOKEN
    ):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


class StartSessionRequest(BaseModel):
    headless: bool = False
    browser_port: int = 9223
    initial_url: str = "about:blank"
    profile: Optional[str] = None
    profile_dir: Optional[str] = None
    smart_filter: bool = True
    enable_proxy: bool = False
    proxy_port: int = 8888
    upstream_proxy: Optional[str] = None
    force_restart: bool = False
    transport: Optional[str] = None  # 'websocket' (default) | 'pipe-windows'


class NavigateRequest(BaseModel):
    url: str


class TraceRequest(BaseModel):
    action: str
    target: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    settle_timeout: float = 1.0


class EvalRequest(BaseModel):
    expression: str


class ReplayRequest(BaseModel):
    requestId: str
    mutate_headers: Optional[Dict[str, str]] = None
    mutate_body: Optional[str] = None
    mutate_url: Optional[str] = None
    allow_write: bool = False


class InterceptRuleModel(BaseModel):
    rule_id: str
    url_pattern: str
    resource_type: Optional[str] = None
    action: str = "fulfill"  # 'continue', 'modify_request', 'fulfill', 'fail'
    mock_status: int = 200
    mock_headers: Optional[Dict[str, str]] = None
    mock_body: Optional[str] = None
    modify_headers: Optional[Dict[str, str]] = None
    modify_body: Optional[str] = None
    fail_reason: str = "Failed"


class CookieModel(BaseModel):
    name: str
    value: str
    domain: Optional[str] = None
    path: str = "/"
    secure: bool = False
    httpOnly: bool = False


class StorageModel(BaseModel):
    storage_type: str = "local"
    key: str
    value: str


class DomMutateModel(BaseModel):
    selector: str
    action: str
    kwargs: Optional[Dict[str, Any]] = None


class ScreenshotModel(BaseModel):
    path: str = "screenshot.png"
    full_page: bool = False


class SmartFilterModel(BaseModel):
    enabled: bool


class DumpDiskModel(BaseModel):
    output_dir: str = "source_dump"


def require_controller(session_id: Optional[str] = None) -> AgentBrowserController:
    global controller
    try:
        return SESSIONS.require(session_id)
    except LookupError:
        pass
    if not controller or not controller._running:
        raise HTTPException(status_code=400, detail="Browser session not active. Call /session/start first.")
    return controller


def _register_default(ctrl: AgentBrowserController) -> str:
    global controller
    controller = ctrl
    return SESSIONS.register(ctrl, make_default=True)


@app.post("/session/start")
async def start_session(req: StartSessionRequest):
    global controller
    if req.browser_port == 0:
        req.browser_port = find_free_port()
    from agent_browser.cdp import resolve_profile_dir
    req_profile_name, req_profile_dir = resolve_profile_dir(profile=req.profile, profile_dir=req.profile_dir)

    if controller and controller._running:
        same_profile = (controller.profile_name == req_profile_name and controller.profile_dir == req_profile_dir)
        same_port = (controller.port == req.browser_port)
        if same_profile and same_port and not req.force_restart:
            return {
                "status": "already_running",
                "port": controller.port,
                "profile_name": controller.profile_name,
                "profile_dir": controller.profile_dir,
                "smart_filter": controller.smart_filter
            }

        # Otherwise, user requested a different profile/port or forced restart: stop existing cleanly
        try:
            await controller.stop()
        except Exception:
            pass
        controller = None

    controller = AgentBrowserController(
        port=req.browser_port,
        headless=req.headless,
        profile=req.profile,
        profile_dir=req.profile_dir,
        smart_filter=req.smart_filter,
        enable_proxy=req.enable_proxy,
        proxy_port=req.proxy_port,
        upstream_proxy=req.upstream_proxy,
        transport=req.transport
    )
    await controller.start(initial_url=req.initial_url)
    session_id = _register_default(controller)
    return {
        "status": "started",
        "session_id": session_id,
        "port": req.browser_port,
        "headless": req.headless,
        "profile_name": controller.profile_name,
        "profile_dir": controller.profile_dir,
        "smart_filter": controller.smart_filter,
        "proxy_enabled": req.enable_proxy,
        "proxy_port": req.proxy_port if req.enable_proxy else None
    }


@app.post("/session/stop")
async def stop_session():
    global controller
    if controller:
        sid = SESSIONS.default_id
        await controller.stop()
        if sid:
            SESSIONS.remove(sid)
        controller = SESSIONS.get()
    return {"status": "stopped"}


# --- Multi-session (isolated profiles on ephemeral ports) ---

@app.get("/sessions")
async def list_sessions():
    return {"sessions": SESSIONS.list_summaries(), "count": len(SESSIONS.ids())}


@app.post("/sessions")
async def create_session(req: StartSessionRequest):
    """Create an additional isolated session (own profile + ephemeral port)."""
    port = req.browser_port or find_free_port()
    if port == 9223:  # caller left the legacy default; allocate instead
        port = find_free_port()
    profile = req.profile or f"session-{port}"
    ctrl = AgentBrowserController(
        port=port, headless=req.headless, profile=profile,
        profile_dir=req.profile_dir, smart_filter=req.smart_filter,
        enable_proxy=req.enable_proxy, proxy_port=req.proxy_port,
        upstream_proxy=req.upstream_proxy,
    )
    await ctrl.start(initial_url=req.initial_url)
    sid = SESSIONS.register(ctrl)
    return {"status": "started", "session_id": sid, "port": port, "profile_name": ctrl.profile_name}


@app.get("/sessions/{session_id}/status")
async def session_by_id_status(session_id: str):
    try:
        ctrl = SESSIONS.require(session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"session_id": session_id, "active": bool(ctrl._running), "port": ctrl.port,
            "profile_name": ctrl.profile_name, "targetId": ctrl.active_target_id}


@app.post("/sessions/{session_id}/navigate")
async def session_by_id_navigate(session_id: str, req: NavigateRequest):
    try:
        ctrl = SESSIONS.require(session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    res = await ctrl.navigate(req.url)
    return {"navigated": bool(res.get("navigated")), "url": res.get("url") or req.url, "error": res.get("error")}


@app.post("/sessions/{session_id}/stop")
async def session_by_id_stop(session_id: str):
    ctrl = SESSIONS.remove(session_id)
    if ctrl is None:
        raise HTTPException(status_code=404, detail="session not found")
    global controller
    await ctrl.stop()
    if controller is ctrl:
        controller = SESSIONS.get()
    return {"status": "stopped", "session_id": session_id}


class ActivateTabModel(BaseModel):
    session_id: str


@app.get("/targets")
async def list_targets():
    """Attached page tabs of the default controller (one can be activated)."""
    ctrl = require_controller()
    return {"targets": ctrl.list_tabs(), "active_session_id": ctrl.active_session_id}


@app.post("/targets/activate")
async def activate_target(m: ActivateTabModel):
    ctrl = require_controller()
    try:
        return ctrl.activate_tab(m.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


class DialogPolicyModel(BaseModel):
    policy: str  # 'accept' | 'dismiss'


@app.get("/dialogs")
async def list_dialogs():
    ctrl = require_controller()
    return {"dialog_policy": ctrl.dialog_policy, "dialogs": ctrl.dialog_log}


@app.post("/dialog/policy")
async def set_dialog_policy(m: DialogPolicyModel):
    ctrl = require_controller()
    policy = m.policy.strip().lower()
    if policy not in ("accept", "dismiss"):
        raise HTTPException(status_code=400, detail="policy must be 'accept' or 'dismiss'")
    ctrl.dialog_policy = policy
    return {"dialog_policy": ctrl.dialog_policy}


@app.get("/downloads")
async def list_downloads():
    ctrl = require_controller()
    return {"downloads": ctrl.downloads}


class HandoffStartModel(BaseModel):
    reason: Optional[str] = None
    instructions: Optional[str] = None


class HandoffCompleteModel(BaseModel):
    summary: Optional[str] = None


@app.get("/handoff")
async def handoff_status():
    ctrl = require_controller()
    return ctrl.handoff_status()


@app.post("/handoff/start")
async def handoff_start(m: HandoffStartModel):
    ctrl = require_controller()
    return await ctrl.start_handoff(reason=m.reason or "", instructions=m.instructions or "")


@app.post("/handoff/complete")
async def handoff_complete(m: HandoffCompleteModel):
    ctrl = require_controller()
    return await ctrl.complete_handoff(summary=m.summary or "")


@app.post("/shutdown")
async def shutdown_daemon():
    """Stop the browser this daemon owns, then exit the daemon process."""
    global controller
    try:
        await SESSIONS.stop_all()
    except Exception:
        pass
    if controller:
        try:
            await controller.stop()
        except Exception:
            pass
        controller = None

    async def _exit_soon():
        await asyncio.sleep(0.2)
        os._exit(0)

    asyncio.create_task(_exit_soon())
    return {"status": "shutting_down"}


@app.get("/session/status")
async def session_status():
    global controller
    is_active = controller is not None and controller._running
    return {
        "active": is_active,
        "sessionId": controller.active_session_id if is_active else None,
        "targetId": controller.active_target_id if is_active else None,
        "profile_name": controller.profile_name if is_active else None,
        "profile_dir": controller.profile_dir if is_active else None,
        "smart_filter": (controller.network.smart_filter if (is_active and controller.network) else None),
        "dropped_events": (controller.cdp.dropped_events if (is_active and controller.cdp) else 0),
    }


@app.get("/profiles")
async def get_profiles():
    global controller
    profiles = list_profiles()
    active_profile = controller.profile_name if (controller and controller._running) else None
    return {
        "active_profile": active_profile,
        "profiles": profiles,
        "count": len(profiles)
    }


@app.post("/navigate")
async def navigate(req: NavigateRequest):
    ctrl = require_controller()
    res = await ctrl.navigate(req.url)
    navigated = bool(res.get("navigated"))
    return {
        "navigated": navigated,
        "url": res.get("url") or req.url,
        "error": res.get("error"),
        "loaded": res.get("loaded"),
        "loaderId": res.get("loaderId"),
        "result": res.get("result"),
    }


@app.get("/agent_view")
async def agent_view():
    ctrl = require_controller()
    text = await ctrl.get_agent_view()
    return {"view": text}


@app.get("/snapshot")
async def snapshot():
    ctrl = require_controller()
    return await ctrl.get_interactive_snapshot()


@app.post("/trace")
async def trace_action(req: TraceRequest):
    ctrl = require_controller()
    report = await ctrl.tracer.trace_action(
        req.action,
        target=req.target,
        params=req.params,
        settle_timeout=req.settle_timeout
    )
    return report


@app.post("/eval")
async def evaluate_js(req: EvalRequest):
    if _EVAL_DISABLED:
        raise HTTPException(status_code=403, detail="eval is disabled (AGENTBROWSER_DISABLE_EVAL)")
    ctrl = require_controller()
    return await ctrl.evaluate(req.expression)


@app.get("/traffic")
async def search_traffic(
    query: Optional[str] = Query(None),
    status: Optional[int] = Query(None),
    mime: Optional[str] = Query(None),
    limit: int = Query(50),
    include_secrets: bool = Query(False)
):
    ctrl = require_controller()
    results = ctrl.network.search_traffic(query=query, status=status, mime=mime, limit=limit)
    if not include_secrets:
        results = [redact_entry(entry) for entry in results]
    return results


@app.post("/replay")
async def replay_request(req: ReplayRequest):
    ctrl = require_controller()
    # replay_request is a blocking sync HTTP call; keep it off the event loop
    # so it cannot starve the CDP reader for up to its full timeout.
    return await asyncio.to_thread(
        ctrl.network.replay_request,
        req.requestId,
        mutate_headers=req.mutate_headers,
        mutate_body=req.mutate_body,
        mutate_url=req.mutate_url,
        allow_write=req.allow_write,
    )


@app.post("/intercept/add")
async def add_intercept_rule(rule_model: InterceptRuleModel):
    ctrl = require_controller()
    rule = InterceptRule(
        rule_id=rule_model.rule_id,
        url_pattern=rule_model.url_pattern,
        resource_type=rule_model.resource_type,
        action=rule_model.action,
        mock_status=rule_model.mock_status,
        mock_headers=rule_model.mock_headers,
        mock_body=rule_model.mock_body,
        modify_headers=rule_model.modify_headers,
        modify_body=rule_model.modify_body,
        fail_reason=rule_model.fail_reason
    )
    ctrl.network.add_rule(rule)
    await ctrl.network.enable_interception()
    return {"status": "rule_added", "rule_id": rule.rule_id}


@app.post("/intercept/clear")
async def clear_intercept_rules():
    ctrl = require_controller()
    ctrl.network.clear_rules()
    await ctrl.network.disable_interception()
    return {"status": "rules_cleared"}


@app.get("/diagnostics")
async def run_diagnostics():
    ctrl = require_controller()
    return await ctrl.diagnostics.run_diagnostics()


@app.get("/perf/vitals")
async def perf_vitals():
    ctrl = require_controller()
    return await ctrl.perf.snapshot()


class DebugEpisodeModel(BaseModel):
    url: str
    action: str = "click"
    target: Optional[str] = None
    verify_runs: int = 3


@app.post("/debug/episode")
async def debug_episode(m: DebugEpisodeModel):
    """
    Run one OBSERVE..VERIFY episode (no fix hook: the after-state is the
    current page, so VERIFY honestly reports BLOCKED when the bug persists).
    """
    from agent_browser.loop_engine import DebugLoop

    ctrl = require_controller()
    return await DebugLoop(ctrl).run_episode(
        m.url, action=m.action, target=m.target, verify_runs=m.verify_runs)


@app.get("/cookies")
async def get_cookies(include_secrets: bool = Query(False)):
    ctrl = require_controller()
    cookies = await ctrl.storage.get_cookies()
    if not include_secrets:
        cookies = [{**c, "value": "[redacted]"} for c in cookies]
    return {"cookies": cookies}


@app.post("/cookies")
async def set_cookie(cookie: CookieModel):
    ctrl = require_controller()
    ok = await ctrl.storage.set_cookie(
        name=cookie.name,
        value=cookie.value,
        domain=cookie.domain,
        path=cookie.path,
        secure=cookie.secure,
        http_only=cookie.httpOnly
    )
    return {"success": ok}


@app.get("/storage")
async def get_storage(storage_type: str = Query("local"), include_secrets: bool = Query(False)):
    ctrl = require_controller()
    data = await ctrl.storage.get_storage(storage_type)
    if not include_secrets and isinstance(data, dict):
        data = {k: ("[redacted]" if _looks_secret(k) else v) for k, v in data.items()}
    return data


@app.post("/storage")
async def set_storage_item(item: StorageModel):
    ctrl = require_controller()
    ok = await ctrl.storage.set_storage_item(item.storage_type, item.key, item.value)
    return {"success": ok}


@app.post("/mutate_dom")
async def mutate_dom(m: DomMutateModel):
    if _EVAL_DISABLED:
        raise HTTPException(status_code=403, detail="DOM mutation is disabled (AGENTBROWSER_DISABLE_EVAL)")
    ctrl = require_controller()
    kwargs = m.kwargs or {}
    return await ctrl.dom.mutate_element(m.selector, m.action, **kwargs)


@app.post("/screenshot")
async def take_screenshot(s: ScreenshotModel):
    ctrl = require_controller()
    try:
        safe_path = security.confine_path(s.path, _OUTPUT_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    path = await ctrl.take_screenshot(output_path=safe_path, full_page=s.full_page)
    return {"screenshot_path": path}


class PinModel(BaseModel):
    selector: str
    alias: Optional[str] = None


class UnpinModel(BaseModel):
    target: str


@app.get("/user/actions")
async def get_user_actions(limit: int = Query(20), event_type: Optional[str] = Query(None)):
    ctrl = require_controller()
    return {"actions": ctrl.user_tracker.get_actions(limit=limit, event_type=event_type)}


@app.get("/user/last_action")
async def get_user_last_action():
    ctrl = require_controller()
    act = ctrl.user_tracker.get_last_action()
    return {"last_action": act}


@app.post("/user/clear_actions")
async def clear_user_actions():
    ctrl = require_controller()
    ctrl.user_tracker.clear()
    return {"status": "cleared"}


@app.post("/focus/pin")
async def pin_element(p: PinModel):
    ctrl = require_controller()
    name = ctrl.focus.pin(p.selector, alias=p.alias)
    return {"status": "pinned", "alias": name, "selector": p.selector}


@app.post("/focus/unpin")
async def unpin_element(u: UnpinModel):
    ctrl = require_controller()
    ok = ctrl.focus.unpin(u.target)
    return {"status": "unpinned" if ok else "not_found", "target": u.target}


@app.get("/focus/view")
async def get_focus_view():
    ctrl = require_controller()
    view = await ctrl.focus.get_focused_view()
    try:
        view["ax_regions"] = await ctrl.focus.get_scoped_focused_view()
    except Exception:
        pass
    return view


@app.post("/focus/clear")
async def clear_focus():
    ctrl = require_controller()
    ctrl.focus.clear()
    return {"status": "cleared"}


@app.get("/network/ai_summary")
async def get_network_ai_summary(limit: int = 25, exclude_assets: bool = True):
    ctrl = require_controller()
    text = ctrl.network.format_ai_summary(limit=limit, exclude_assets=exclude_assets)
    return {"summary": text}


@app.get("/network/filter")
async def get_smart_filter():
    ctrl = require_controller()
    return {"smart_filter": ctrl.network.smart_filter}


@app.post("/network/filter")
async def set_smart_filter(m: SmartFilterModel):
    ctrl = require_controller()
    ctrl.network.set_smart_filter(m.enabled)
    ctrl.smart_filter = m.enabled
    return {"status": "ok", "smart_filter": ctrl.network.smart_filter}


@app.get("/source/html")
async def get_page_html():
    ctrl = require_controller()
    html = await ctrl.source_inspector.get_page_html()
    return {"html": html, "length": len(html)}


@app.get("/source/dump")
async def dump_all_sources(include_content: bool = True):
    ctrl = require_controller()
    bundle = await ctrl.source_inspector.get_all_sources(include_content=include_content)
    return bundle


@app.post("/source/dump_to_disk")
async def dump_sources_to_disk(m: DumpDiskModel):
    ctrl = require_controller()
    try:
        safe_dir = security.confine_path(m.output_dir, _OUTPUT_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    res = await ctrl.source_inspector.dump_sources_to_disk(safe_dir)
    return res


@app.get("/source/scripts")
async def list_scripts(include_inline: bool = True):
    ctrl = require_controller()
    scripts = await ctrl.source_inspector.list_all_scripts(include_inline=include_inline)
    return {"scripts": scripts, "count": len(scripts)}


@app.get("/source/code")
async def get_script_source(script_id: str):
    ctrl = require_controller()
    code = await ctrl.source_inspector.get_script_source(script_id)
    if code is None:
        raise HTTPException(status_code=404, detail=f"Script ID '{script_id}' not found")
    return {"scriptId": script_id, "source": code}


@app.get("/source/reconstruct")
async def reconstruct_sources():
    ctrl = require_controller()
    tree = await ctrl.source_inspector.reconstruct_sources()
    # Contents can be large; return the tree + endpoints without bodies by
    # default (files carry paths/line counts; fetch script-source for code).
    slim = [{k: v for k, v in f.items() if k != "content"} for f in tree.get("files", [])]
    return {**tree, "files": slim}


@app.get("/source/find_crypto")
async def find_crypto_in_scripts(script_id: Optional[str] = None):
    ctrl = require_controller()
    findings = await ctrl.source_inspector.find_crypto_and_api_functions(script_id=script_id)
    return {"findings": findings, "count": len(findings)}


@app.get("/source/crypto_logs")
async def get_crypto_hook_logs(include_secrets: bool = Query(False)):
    ctrl = require_controller()
    logs = await ctrl.source_inspector.collect_crypto_hook_logs()
    if not include_secrets:
        # Pre-encryption plaintext/keys are the highest-sensitivity surface:
        # default to a redacted shape, explicit opt-in returns raw logs.
        redacted_logs = [
            {**log, "details": "[redacted]"} if isinstance(log, dict) else log for log in logs
        ]
        return {"crypto_logs": redacted_logs, "count": len(logs), "redacted": True}
    return {"crypto_logs": logs, "count": len(logs)}


@app.get("/source/caller_for_request")
async def get_request_caller(request_id: str, include_secrets: bool = Query(False)):
    ctrl = require_controller()
    req = ctrl.network.get_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail=f"Request '{request_id}' not found")
    req = await ctrl.network.await_body(request_id) or req
    if req.get("response_body") is None:
        logs = await ctrl.source_inspector.collect_crypto_hook_logs()
        target = req.get("url") or ""
        for item in reversed(logs):
            if item.get("kind") != "fetch_response":
                continue
            details = item.get("details") or {}
            logged = details.get("url") or ""
            if logged and (logged in target or target.endswith(logged)):
                req["response_body"] = details.get("body")
                req["response_body_source"] = "page_fetch_hook"
                break
    caller_info = await ctrl.source_inspector.get_caller_context_for_request(req.get("initiator", {}))
    return {"request": req if include_secrets else redact_entry(req), "caller": caller_info}


@app.get("/proxy/traffic")
async def get_proxy_traffic(query: Optional[str] = None, limit: int = 50, include_secrets: bool = Query(False)):
    ctrl = require_controller()
    if not ctrl.proxy_server:
        return {"status": "proxy_disabled", "flows": []}
    flows = ctrl.proxy_server.hub.get_flows(query=query, limit=limit)
    if not include_secrets:
        flows = [redact_entry(flow) for flow in flows]
    return {"status": "active", "count": len(flows), "flows": flows}


def run_server(host: str = "127.0.0.1", port: int = 9224):
    global DAEMON_TOKEN
    DAEMON_TOKEN = security.load_or_create_token(port)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AgentBrowser daemon")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9224)
    ns = parser.parse_args()
    run_server(host=ns.host, port=ns.port)
