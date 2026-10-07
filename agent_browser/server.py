import os
import sys
import asyncio
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
import uvicorn

from agent_browser.agent_api import AgentBrowserController
from agent_browser.network import InterceptRule

app = FastAPI(title="AgentBrowser Debugger API", version="1.0.0")

controller: Optional[AgentBrowserController] = None


class StartSessionRequest(BaseModel):
    headless: bool = False
    browser_port: int = 9223
    initial_url: str = "about:blank"
    profile_dir: Optional[str] = None


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


def require_controller() -> AgentBrowserController:
    global controller
    if not controller or not controller._running:
        raise HTTPException(status_code=400, detail="Browser session not active. Call /session/start first.")
    return controller


@app.post("/session/start")
async def start_session(req: StartSessionRequest):
    global controller
    if controller and controller._running:
        return {"status": "already_running", "port": controller.port}

    controller = AgentBrowserController(
        port=req.browser_port,
        headless=req.headless,
        profile_dir=req.profile_dir
    )
    await controller.start(initial_url=req.initial_url)
    return {"status": "started", "port": req.browser_port, "headless": req.headless}


@app.post("/session/stop")
async def stop_session():
    global controller
    if controller:
        await controller.stop()
        controller = None
    return {"status": "stopped"}


@app.get("/session/status")
async def session_status():
    global controller
    is_active = controller is not None and controller._running
    return {
        "active": is_active,
        "sessionId": controller.active_session_id if is_active else None,
        "targetId": controller.active_target_id if is_active else None
    }


@app.post("/navigate")
async def navigate(req: NavigateRequest):
    ctrl = require_controller()
    res = await ctrl.navigate(req.url)
    return {"navigated": True, "url": req.url, "result": res}


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
    ctrl = require_controller()
    return await ctrl.evaluate(req.expression)


@app.get("/traffic")
async def search_traffic(
    query: Optional[str] = Query(None),
    status: Optional[int] = Query(None),
    mime: Optional[str] = Query(None),
    limit: int = Query(50)
):
    ctrl = require_controller()
    return ctrl.network.search_traffic(query=query, status=status, mime=mime, limit=limit)


@app.post("/replay")
async def replay_request(req: ReplayRequest):
    ctrl = require_controller()
    return ctrl.network.replay_request(
        req.requestId,
        mutate_headers=req.mutate_headers,
        mutate_body=req.mutate_body,
        mutate_url=req.mutate_url
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


@app.get("/cookies")
async def get_cookies():
    ctrl = require_controller()
    cookies = await ctrl.storage.get_cookies()
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
async def get_storage(storage_type: str = Query("local")):
    ctrl = require_controller()
    return await ctrl.storage.get_storage(storage_type)


@app.post("/storage")
async def set_storage_item(item: StorageModel):
    ctrl = require_controller()
    ok = await ctrl.storage.set_storage_item(item.storage_type, item.key, item.value)
    return {"success": ok}


@app.post("/mutate_dom")
async def mutate_dom(m: DomMutateModel):
    ctrl = require_controller()
    kwargs = m.kwargs or {}
    return await ctrl.dom.mutate_element(m.selector, m.action, **kwargs)


@app.post("/screenshot")
async def take_screenshot(s: ScreenshotModel):
    ctrl = require_controller()
    path = await ctrl.take_screenshot(output_path=s.path, full_page=s.full_page)
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
    return await ctrl.focus.get_focused_view()


@app.post("/focus/clear")
async def clear_focus():
    ctrl = require_controller()
    ctrl.focus.clear()
    return {"status": "cleared"}


def run_server(host: str = "127.0.0.1", port: int = 9224):
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run_server()
