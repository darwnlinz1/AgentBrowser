"""
Phase 2 tests: session registry, AXTree refs/Merkle diff, MCP tool catalog.
No browser is launched (AX snapshot uses a fake CDP).
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.axtree import build_ref_table, diff_refs, estimate_tokens, merkle_hashes, render_view
from agent_browser.dom import DOMEngine
from agent_browser.mcp_server import LocalSessionManager, MCPDispatcher, NEVER_EXPOSED, build_server, list_tools
from agent_browser.session_registry import SessionRegistry, find_free_port


class _FakeCtrl:
    def __init__(self, port):
        self._running = True
        self.port = port
        self.profile_name = f"p{port}"
        self.active_target_id = f"t{port}"


def test_registry_default_and_isolation():
    reg = SessionRegistry()
    a, b = _FakeCtrl(1), _FakeCtrl(2)
    ida = reg.register(a, make_default=True)
    idb = reg.register(b)
    assert reg.get() is a
    assert reg.get(idb) is b
    assert reg.require(idb) is b
    reg.remove(ida)
    assert reg.get() is b  # default falls through to remaining session
    with pytest.raises(LookupError):
        reg.require("missing")


def test_find_free_port_is_ephemeral_and_distinct():
    ports = {find_free_port() for _ in range(5)}
    assert len(ports) >= 4
    assert all(p > 0 for p in ports)


_AX_NODES = [
    {"nodeId": "1", "backendDOMNodeId": 10, "role": {"value": "RootWebArea"}, "name": {"value": "Shop"}, "childIds": ["2", "3"]},
    {"nodeId": "2", "parentId": "1", "backendDOMNodeId": 11, "role": {"value": "button"}, "name": {"value": "Buy now"}, "childIds": []},
    {"nodeId": "3", "parentId": "1", "backendDOMNodeId": 12, "role": {"value": "textbox"}, "name": {"value": "Search"}, "childIds": [],
     "properties": [{"name": "disabled", "value": {"value": True}}]},
    {"nodeId": "4", "parentId": "1", "backendDOMNodeId": 13, "role": {"value": "generic"}, "name": {"value": ""}, "childIds": []},
]


def test_axtree_refs_bind_backend_ids_and_render_lean():
    refs = build_ref_table(_AX_NODES)
    assert [r["role"] for r in refs] == ["button", "textbox"]  # generic + root excluded
    assert refs[0]["backend_id"] == 11 and refs[0]["ref"] == 1
    assert refs[1]["disabled"] is True
    view = render_view(refs, title="Shop", url="http://x/")
    assert "[#1] button 'Buy now'" in view
    assert estimate_tokens(view) < 120


def test_budgeted_keyframe_view_enforces_token_budget():
    from agent_browser.axtree import estimate_tokens, render_budgeted_view
    refs = build_ref_table(_AX_NODES * 60)  # many repeated actionable nodes
    view = render_budgeted_view(refs, title="Shop", url="http://x/", budget_tokens=200)
    assert estimate_tokens(view) <= 220
    assert "more actionable nodes" in view
    small = render_budgeted_view(build_ref_table(_AX_NODES), title="Shop", url="http://x/")
    assert "Buy now" in small


def test_merkle_hash_propagates_and_diff_is_minimal():
    h1 = merkle_hashes(_AX_NODES)
    changed = [dict(n) for n in _AX_NODES]
    changed[1] = {**changed[1], "name": {"value": "Buy later"}}
    h2 = merkle_hashes(changed)
    assert h1["2"] != h2["2"]          # leaf changed
    assert h1["1"] != h2["1"]          # ancestor hash changed (subtree)
    assert h1["3"] == h2["3"]          # sibling subtree unchanged

    old = build_ref_table(_AX_NODES)
    new = build_ref_table(changed)
    diff = diff_refs(old, new)
    assert len(diff["changed"]) == 1
    assert diff["changed"][0]["after"]["name"] == "Buy later"
    assert diff["added"] == [] and diff["removed"] == []


class _FakeCDP:
    def __init__(self):
        self.sent = []

    async def send(self, method, params=None, session_id=None, timeout=15):
        self.sent.append(method)
        if method == "Accessibility.getFullAXTree":
            return {"nodes": _AX_NODES}
        if method == "Runtime.evaluate":
            return {"result": {"value": {"t": "Shop", "u": "http://x/"}}}
        return {}


@pytest.mark.asyncio
async def test_dom_axtree_snapshot_no_page_pollution():
    dom = DOMEngine(_FakeCDP(), lambda: "sid")
    snap = await dom.get_axtree_snapshot()
    assert snap["count"] == 2
    assert snap["merkle_root"]
    assert snap["refs"][0]["backend_id"] == 11
    # Second snapshot diffs against the first.
    snap2 = await dom.get_axtree_snapshot()
    assert snap2["diff_from_previous"]["unchanged_count"] == 2


def test_mcp_catalog_tiers_and_never_exposed():
    tools = list_tools()
    names = {t["name"] for t in tools}
    assert len(tools) >= 16
    assert not (names & set(NEVER_EXPOSED))
    assert "eval" not in names and "shutdown" not in names
    inspection = {t["name"] for t in list_tools(["inspection"])}
    assert "snapshot" in inspection and "act_click" not in inspection
    read_only = {t["name"]: t["annotations"]["readOnlyHint"] for t in tools}
    assert read_only["diagnose"] is False  # diagnostics tier is not marked read-only inspection
    assert read_only["traffic_search"] is True


class _FakeSessionCtrl:
    def __init__(self, port):
        self._running = False
        self.port = port
        self.profile_name = f"p{port}"
        self.active_target_id = None
        self.started_with = None

    async def start(self, initial_url="about:blank"):
        self._running = True
        self.started_with = initial_url

    async def stop(self):
        self._running = False


@pytest.mark.asyncio
async def test_mcp_session_manager_lifecycle():
    made = []

    def factory(args):
        ctrl = _FakeSessionCtrl(9700 + len(made))
        made.append(ctrl)
        return ctrl

    mgr = LocalSessionManager(controller_factory=factory)
    disp = MCPDispatcher(mgr.get_controller, session_manager=mgr)

    status0 = await disp.call_tool("session_status", {})
    assert status0["active"] is False

    opened = await disp.call_tool("session_open", {"profile": "x"})
    sid = opened["session_id"]
    assert made[0]._running is True
    status = await disp.call_tool("session_status", {"session_id": sid})
    assert status["active"] is True and status["port"] == 9700
    assert mgr.get_controller(sid) is made[0]

    opened2 = await disp.call_tool("session_open", {})
    assert opened2["session_id"] != sid
    closed = await disp.call_tool("session_close", {"session_id": sid})
    assert closed["status"] == "closed" and made[0]._running is False
    # Default now falls through to the remaining session.
    assert mgr.get_controller() is made[1]


@pytest.mark.asyncio
async def test_mcp_build_server_registers_tools():
    mgr = LocalSessionManager(controller_factory=lambda args: _FakeSessionCtrl(9800))
    server = build_server(mgr.get_controller, session_manager=mgr)
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "snapshot" in names and "diagnose" in names
    assert "eval" not in names and "shutdown" not in names
    assert len(names) == len(list_tools())


@pytest.mark.asyncio
async def test_mcp_tab_tools():
    class FakeCtrl:
        active_session_id = "s1"

        def list_tabs(self):
            return [{"session_id": "s1", "active": True}, {"session_id": "s2", "active": False}]

        def activate_tab(self, sid):
            self.active_session_id = sid
            return {"active_session_id": sid}

    ctrl = FakeCtrl()
    disp = MCPDispatcher(lambda sid: ctrl)
    out = await disp.call_tool("tabs_list", {})
    assert out["tabs"][1]["session_id"] == "s2"
    out = await disp.call_tool("tab_activate", {"tab_session_id": "s2"})
    assert out["active_session_id"] == "s2"
    names = {t["name"] for t in list_tools()}
    assert "tabs_list" in names and "tab_activate" in names


@pytest.mark.asyncio
async def test_mcp_dispatcher_rejects_eval_and_routes_calls():
    class FakeNet:
        def search_traffic(self, query=None, status=None, limit=20):
            return [{"id": "1", "url": "https://api.example/me", "headers": {"Authorization": "Bearer secret"}}]

        def format_ai_summary(self, limit=25):
            return "summary"

    class FakeCtrl:
        network = FakeNet()

    disp = MCPDispatcher(lambda sid: FakeCtrl())
    with pytest.raises(ValueError):
        await disp.call_tool("eval", {"expression": "1"})
    out = await disp.call_tool("traffic_search", {"query": "me"})
    assert out["count"] == 1
    assert out["requests"][0]["headers"]["Authorization"] == "[redacted]"
