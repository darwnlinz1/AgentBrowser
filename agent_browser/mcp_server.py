"""
AgentBrowser as a debugging-native Model Context Protocol (MCP) server.

Design rules (from the Phase 2 research):
- A curated tool set (~18), split into capability tiers, not an auto-wrap of
  every HTTP route. ``eval`` and ``shutdown`` are NEVER exposed.
- Outputs are redacted by default (traffic/cookies go through the same
  on-egress redaction as the HTTP API).
- Sessions are explicit handles (``session_id``), matching the stateless MCP
  direction: the server hands out handles instead of relying on a hidden
  global session.

The dispatcher below is transport-independent and unit-testable with a fake
controller. When the optional ``mcp`` package is installed, ``build_server``
wraps the same registry in a FastMCP stdio/HTTP server.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

TIER_INSPECTION = "inspection"      # read-only
TIER_MUTATION = "mutation"          # acts on the page
TIER_INTERCEPTION = "interception"  # network rules (dev targets only)
TIER_DIAGNOSTICS = "diagnostics"    # causal analysis + repro

ALL_TIERS = (TIER_INSPECTION, TIER_MUTATION, TIER_INTERCEPTION, TIER_DIAGNOSTICS)

# name -> {tier, description, properties}
TOOL_SPECS: Dict[str, Dict[str, Any]] = {
    "session_open": {"tier": TIER_MUTATION, "description": "Open an isolated browser session; returns a session_id handle.", "properties": {"headless": {"type": "boolean"}, "profile": {"type": "string"}}},
    "session_close": {"tier": TIER_MUTATION, "description": "Close a session by handle.", "properties": {"session_id": {"type": "string"}}},
    "session_status": {"tier": TIER_INSPECTION, "description": "Status of a session (default session when omitted).", "properties": {"session_id": {"type": "string"}}},
    "navigate": {"tier": TIER_MUTATION, "description": "Navigate a session to a URL.", "properties": {"url": {"type": "string"}, "session_id": {"type": "string"}}, "required": ["url"]},
    "snapshot": {"tier": TIER_INSPECTION, "description": "Accessibility-tree action graph with node-identity refs.", "properties": {"session_id": {"type": "string"}}},
    "act_click": {"tier": TIER_MUTATION, "description": "Click an element by selector or #ref, with causal trace.", "properties": {"target": {"type": "string"}, "session_id": {"type": "string"}}, "required": ["target"]},
    "act_type": {"tier": TIER_MUTATION, "description": "Type text into an element, with causal trace.", "properties": {"target": {"type": "string"}, "text": {"type": "string"}, "session_id": {"type": "string"}}, "required": ["target", "text"]},
    "traffic_search": {"tier": TIER_INSPECTION, "description": "Search captured traffic (redacted unless include_secrets).", "properties": {"query": {"type": "string"}, "status": {"type": "integer"}, "limit": {"type": "integer"}, "session_id": {"type": "string"}}},
    "traffic_summary": {"tier": TIER_INSPECTION, "description": "Token-lean AI summary of captured traffic.", "properties": {"limit": {"type": "integer"}, "session_id": {"type": "string"}}},
    "diagnose": {"tier": TIER_DIAGNOSTICS, "description": "Root-cause diagnostics with evidence-graded hypotheses.", "properties": {"session_id": {"type": "string"}}},
    "console": {"tier": TIER_INSPECTION, "description": "Recent console messages and uncaught exceptions.", "properties": {"level": {"type": "string"}, "session_id": {"type": "string"}}},
    "caller": {"tier": TIER_INSPECTION, "description": "Map a captured request to the JS caller / initiator chain.", "properties": {"request_id": {"type": "string"}, "session_id": {"type": "string"}}, "required": ["request_id"]},
    "source_find": {"tier": TIER_INSPECTION, "description": "List loaded scripts or fetch one script's source.", "properties": {"script_id": {"type": "string"}, "session_id": {"type": "string"}}},
    "mock_add": {"tier": TIER_INTERCEPTION, "description": "Add a request mock/modify rule (authorized dev targets only).", "properties": {"url_pattern": {"type": "string"}, "mock_body": {"type": "string"}, "mock_status": {"type": "integer"}, "session_id": {"type": "string"}}, "required": ["url_pattern"]},
    "mock_clear": {"tier": TIER_INTERCEPTION, "description": "Clear all interception rules.", "properties": {"session_id": {"type": "string"}}},
    "replay": {"tier": TIER_DIAGNOSTICS, "description": "Replay a captured request (in-browser when available) and diff the response.", "properties": {"request_id": {"type": "string"}, "session_id": {"type": "string"}}, "required": ["request_id"]},
    "repro_generate": {"tier": TIER_DIAGNOSTICS, "description": "Generate a failure-signature regression test for an action.", "properties": {"url": {"type": "string"}, "target": {"type": "string"}, "session_id": {"type": "string"}}, "required": ["url", "target"]},
    "user_actions": {"tier": TIER_INSPECTION, "description": "Recent human actions captured for co-debugging.", "properties": {"limit": {"type": "integer"}, "session_id": {"type": "string"}}},
    "perf_vitals": {"tier": TIER_DIAGNOSTICS, "description": "Web-vitals snapshot (LCP/CLS/long-tasks, graded) + renderer metrics.", "properties": {"session_id": {"type": "string"}}},
    "tabs_list": {"tier": TIER_INSPECTION, "description": "List page tabs attached to a session's browser.", "properties": {"session_id": {"type": "string"}}},
    "tab_activate": {"tier": TIER_MUTATION, "description": "Make a page tab (from tabs_list) the active tab for subsequent tools.", "properties": {"tab_session_id": {"type": "string"}, "session_id": {"type": "string"}}, "required": ["tab_session_id"]},
}

NEVER_EXPOSED = ("eval", "shutdown", "eval_js", "dom_mutate")


def list_tools(tiers: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    wanted = set(tiers) if tiers else set(ALL_TIERS)
    tools = []
    for name, spec in TOOL_SPECS.items():
        if spec["tier"] not in wanted:
            continue
        tools.append({
            "name": name,
            "description": spec["description"],
            "tier": spec["tier"],
            "inputSchema": {
                "type": "object",
                "properties": spec.get("properties") or {},
                "required": spec.get("required") or [],
            },
            "annotations": {"readOnlyHint": spec["tier"] == TIER_INSPECTION},
        })
    return tools


class LocalSessionManager:
    """
    Owns browser sessions for a standalone MCP server process, backed by the
    shared SessionRegistry (isolated profile + ephemeral port per session).
    ``controller_factory(args)`` is injectable for tests.
    """

    def __init__(self, controller_factory: Optional[Callable[[Dict[str, Any]], Any]] = None):
        from agent_browser.session_registry import SessionRegistry

        self.registry = SessionRegistry()
        self._factory = controller_factory or self._default_factory

    @staticmethod
    def _default_factory(args: Dict[str, Any]):
        from agent_browser.agent_api import AgentBrowserController
        from agent_browser.session_registry import find_free_port

        return AgentBrowserController(
            port=find_free_port(),
            headless=bool(args.get("headless", True)),
            profile=args.get("profile"),
        )

    async def session_open(self, args: Dict[str, Any]) -> Dict[str, Any]:
        ctrl = self._factory(args)
        await ctrl.start(initial_url=args.get("url") or "about:blank")
        sid = self.registry.register(ctrl, make_default=len(self.registry.ids()) == 0)
        return {"session_id": sid, "port": getattr(ctrl, "port", None),
                "profile_name": getattr(ctrl, "profile_name", None)}

    async def session_close(self, args: Dict[str, Any]) -> Dict[str, Any]:
        sid = args.get("session_id") or self.registry.default_id
        if not sid:
            return {"status": "no_session"}
        ctrl = self.registry.remove(sid)
        if ctrl is None:
            raise ValueError(f"session not found: {sid}")
        await ctrl.stop()
        return {"status": "closed", "session_id": sid}

    async def session_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        ctrl = self.registry.get(args.get("session_id"))
        return {
            "active": bool(ctrl is not None and getattr(ctrl, "_running", False)),
            "port": getattr(ctrl, "port", None) if ctrl else None,
            "profile_name": getattr(ctrl, "profile_name", None) if ctrl else None,
            "target_id": getattr(ctrl, "active_target_id", None) if ctrl else None,
            "sessions": self.registry.list_summaries(),
        }

    def get_controller(self, session_id: Optional[str] = None):
        try:
            return self.registry.require(session_id)
        except LookupError:
            raise RuntimeError("No browser session open; call session_open first.")


class MCPDispatcher:
    """
    Maps MCP tool calls onto an AgentBrowserController (or the session
    registry). ``controller_provider(session_id)`` must return a controller.
    Session lifecycle tools delegate to ``session_manager`` when supplied.
    """

    def __init__(self, controller_provider: Callable[[Optional[str]], Any], session_manager: Any = None):
        self._provider = controller_provider
        self._sessions = session_manager

    def _ctrl(self, args: Dict[str, Any]):
        return self._provider(args.get("session_id"))

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        args = arguments or {}
        if name in NEVER_EXPOSED or name not in TOOL_SPECS:
            raise ValueError(f"tool not available: {name}")
        if self._sessions is not None and name in ("session_open", "session_close", "session_status"):
            return await getattr(self._sessions, name)(args)
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise ValueError(f"tool not implemented: {name}")
        return await handler(args)

    # -- inspection -------------------------------------------------------
    async def _tool_perf_vitals(self, args):
        ctrl = self._ctrl(args)
        return await ctrl.perf.snapshot()

    async def _tool_tabs_list(self, args):
        ctrl = self._ctrl(args)
        return {"tabs": ctrl.list_tabs(), "active_session_id": ctrl.active_session_id}

    async def _tool_tab_activate(self, args):
        ctrl = self._ctrl(args)
        return ctrl.activate_tab(args["tab_session_id"])

    async def _tool_session_status(self, args):
        ctrl = self._ctrl(args)
        return {"active": bool(getattr(ctrl, "_running", False)), "port": getattr(ctrl, "port", None),
                "profile_name": getattr(ctrl, "profile_name", None), "target_id": getattr(ctrl, "active_target_id", None)}

    async def _tool_snapshot(self, args):
        ctrl = self._ctrl(args)
        if hasattr(ctrl.dom, "format_axtree_view"):
            return {"view": await ctrl.dom.format_axtree_view()}
        return {"view": await ctrl.get_agent_view()}

    async def _tool_traffic_search(self, args):
        from agent_browser.evidence import redact_entry
        ctrl = self._ctrl(args)
        rows = ctrl.network.search_traffic(query=args.get("query"), status=args.get("status"), limit=int(args.get("limit") or 20))
        return {"requests": [redact_entry(r) for r in rows], "count": len(rows)}

    async def _tool_traffic_summary(self, args):
        ctrl = self._ctrl(args)
        return {"summary": ctrl.network.format_ai_summary(limit=int(args.get("limit") or 25))}

    async def _tool_console(self, args):
        ctrl = self._ctrl(args)
        return {"messages": ctrl.runtime.get_console_messages(level=args.get("level"), limit=50),
                "exceptions": ctrl.runtime.get_exceptions(limit=20)}

    async def _tool_caller(self, args):
        ctrl = self._ctrl(args)
        req = ctrl.network.get_request(args["request_id"])
        if not req:
            raise ValueError(f"request not found: {args['request_id']}")
        return await ctrl.source_inspector.get_caller_context_for_request(req.get("initiator", {}))

    async def _tool_source_find(self, args):
        ctrl = self._ctrl(args)
        if args.get("script_id"):
            return {"source": await ctrl.source_inspector.get_script_source(args["script_id"])}
        scripts = await ctrl.source_inspector.list_all_scripts()
        return {"scripts": scripts, "count": len(scripts)}

    async def _tool_user_actions(self, args):
        ctrl = self._ctrl(args)
        return {"actions": ctrl.user_tracker.get_actions(limit=int(args.get("limit") or 20))}

    # -- mutation ---------------------------------------------------------
    async def _tool_navigate(self, args):
        ctrl = self._ctrl(args)
        res = await ctrl.navigate(args["url"])
        return {"navigated": bool(res.get("navigated")), "url": res.get("url") or args["url"], "error": res.get("error")}

    async def _tool_act_click(self, args):
        ctrl = self._ctrl(args)
        return await ctrl.tracer.trace_action("click", target=args["target"])

    async def _tool_act_type(self, args):
        ctrl = self._ctrl(args)
        return await ctrl.tracer.trace_action("type", target=args["target"], params={"text": args.get("text", "")})

    async def _tool_session_open(self, args):
        raise ValueError("session_open is handled by the transport layer (needs the session registry)")

    async def _tool_session_close(self, args):
        raise ValueError("session_close is handled by the transport layer (needs the session registry)")

    # -- interception -----------------------------------------------------
    async def _tool_mock_add(self, args):
        from agent_browser.network import InterceptRule
        ctrl = self._ctrl(args)
        rule = InterceptRule(rule_id=f"mcp-{abs(hash(args['url_pattern'])) % 10**8}",
                             url_pattern=args["url_pattern"], action="fulfill",
                             mock_status=int(args.get("mock_status") or 200),
                             mock_body=args.get("mock_body") or "{}")
        ctrl.network.add_rule(rule)
        await ctrl.network.enable_interception()
        return {"status": "rule_added", "rule_id": rule.rule_id}

    async def _tool_mock_clear(self, args):
        ctrl = self._ctrl(args)
        ctrl.network.clear_rules()
        await ctrl.network.disable_interception()
        return {"status": "rules_cleared"}

    # -- diagnostics ------------------------------------------------------
    async def _tool_diagnose(self, args):
        ctrl = self._ctrl(args)
        return await ctrl.diagnostics.run_diagnostics()

    async def _tool_replay(self, args):
        ctrl = self._ctrl(args)
        if hasattr(ctrl.network, "replay_in_page_fetch"):
            return await ctrl.network.replay_in_page_fetch(args["request_id"])
        return ctrl.network.replay_request(args["request_id"])

    async def _tool_repro_generate(self, args):
        ctrl = self._ctrl(args)
        script = await ctrl.diagnostics.generate_regression_test(
            args["url"], {"type": "click", "target": args["target"]})
        return {"script": script}


def build_server(controller_provider: Callable[[Optional[str]], Any], session_manager: Any = None):
    """Wrap the registry in a FastMCP server (requires the optional ``mcp`` extra)."""
    try:
        # SDK v2 renamed FastMCP -> MCPServer; support both majors.
        try:
            from mcp.server.mcpserver import MCPServer as _ServerCls
        except Exception:
            from mcp.server.fastmcp import FastMCP as _ServerCls
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("MCP server requires the optional 'mcp' package: pip install 'agent-browser[mcp]'") from exc

    dispatcher = MCPDispatcher(controller_provider, session_manager=session_manager)
    server = _ServerCls("agentbrowser")

    # The SDK derives each tool's input schema from the function signature, so
    # every handler gets an explicit signature built from the tool spec (a
    # bare **kwargs would be wrapped as a single required 'kwargs' field).
    import inspect as _inspect

    _TYPEMAP = {"string": str, "integer": int, "number": float, "boolean": bool}

    for tool in list_tools():
        name = tool["name"]
        schema = tool["inputSchema"]
        required = set(schema.get("required") or [])
        params = []
        annotations = {}
        for prop, pspec in (schema.get("properties") or {}).items():
            py_type = _TYPEMAP.get((pspec or {}).get("type"), str)
            annotations[prop] = py_type
            if prop in required:
                params.append(_inspect.Parameter(prop, _inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=py_type))
            else:
                params.append(_inspect.Parameter(prop, _inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=py_type, default=None))

        def _make(tool_name, sig_params, sig_annotations):
            async def _run(**kwargs):
                return await dispatcher.call_tool(tool_name, {k: v for k, v in kwargs.items() if v is not None})
            _run.__name__ = tool_name
            _run.__signature__ = _inspect.Signature(sig_params)
            _run.__annotations__ = dict(sig_annotations)
            return _run

        server.tool(name=name, description=tool["description"])(_make(name, params, annotations))
    return server


def main() -> None:  # pragma: no cover - entry point
    import asyncio
    import sys

    if "--list-tools" in sys.argv:
        import json as _json
        print(_json.dumps(list_tools(), indent=2))
        return

    # Standalone stdio transport: sessions are owned by this process and
    # backed by the SessionRegistry (open one with the session_open tool).
    manager = LocalSessionManager()
    server = build_server(manager.get_controller, session_manager=manager)
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
