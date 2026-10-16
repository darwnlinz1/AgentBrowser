"""
Seeded fault corpus: 10 local, deterministic fault archetypes.

Each case is self-contained HTML + JSON endpoints served by
``fixtures.fixture_server`` on an ephemeral loopback port. Every case carries
ground truth (the request an action truly triggers, background noise that must
stay ``temporal``, and the expected exception signature) so the engine can score
attribution precision/recall without guessing.

Scope: local loopback only. No external hosts, no real credentials.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Endpoint spec: path -> {status, headers, body, delay_ms}
Endpoint = Dict[str, Any]
Case = Dict[str, Any]


def _page(title: str, body: str, script: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body>{body}"
        f"<script>{script}</script></body></html>"
    )


CASES: List[Case] = [
    {
        "id": "cors-preflight",
        "title": "CORS Preflight Failure",
        "category": "network-policy",
        "html": _page(
            "CORS Preflight Failure",
            "<button id='go'>Load cross-origin</button><div id='out'>idle</div>",
            "document.getElementById('go').onclick=async()=>{"
            "try{await fetch('/api/cors-data',{method:'PUT',headers:{'X-Custom':'1'}});"
            "document.getElementById('out').textContent='ok';}"
            "catch(e){document.getElementById('out').textContent='cors-blocked';}};",
        ),
        "endpoints": {
            "/api/cors-data": {"status": 200, "headers": {"Content-Type": "application/json"}, "body": "{}"},
        },
        "ground_truth": {
            "action": {"type": "click", "target": "#go"},
            "triggered_request": "/api/cors-data",
            "background_requests": [],
            "exception_signature": None,
            "expected_link": "none",
            "failing_status": None,
            "note": "Browser blocks on missing Access-Control-Allow-Origin; Audits/blockedReason is the signal, not console text.",
        },
    },
    {
        "id": "two-fetch-race",
        "title": "Two-Fetch Race Condition",
        "category": "race",
        "html": _page(
            "Two-Fetch Race",
            "<button id='go'>Race</button><div id='out'>idle</div>",
            "document.getElementById('go').onclick=async()=>{"
            "const slow=fetch('/api/slow').then(r=>r.json());"
            "const fast=fetch('/api/fast').then(r=>r.json());"
            "const s=await slow; const f=await fast;"
            "document.getElementById('out').textContent=f.value+':'+s.value;};",
        ),
        "endpoints": {
            "/api/slow": {"status": 200, "headers": {"Content-Type": "application/json"}, "body": '{"value":"slow"}', "delay_ms": 250},
            "/api/fast": {"status": 200, "headers": {"Content-Type": "application/json"}, "body": '{"value":"fast"}', "delay_ms": 10},
        },
        "ground_truth": {
            "action": {"type": "click", "target": "#go"},
            "triggered_request": "/api/fast",
            "triggered_requests": ["/api/fast", "/api/slow"],
            "background_requests": [],
            "exception_signature": None,
            "expected_link": "temporal",
            "failing_status": None,
            "note": "Both fetches are action-triggered; ordering (fast before slow) is the race signature.",
        },
    },
    {
        "id": "debounced-input",
        "title": "Debounced Input Miss",
        "category": "timing",
        "html": _page(
            "Debounced Input",
            "<input id='q' placeholder='search'><div id='out'>0</div>",
            "let n=0,t=null;const out=document.getElementById('out');"
            "document.getElementById('q').addEventListener('input',()=>{"
            "clearTimeout(t);t=setTimeout(async()=>{n++;"
            "await fetch('/api/search?q='+n);out.textContent=String(n);},120);});",
        ),
        "endpoints": {
            "/api/search": {"status": 200, "headers": {"Content-Type": "application/json"}, "body": '{"hits":1}'},
        },
        "ground_truth": {
            "action": {"type": "type", "target": "#q"},
            "triggered_request": "/api/search",
            "background_requests": [],
            "exception_signature": None,
            "expected_link": "temporal",
            "failing_status": None,
            "note": "Settle must wait past the 120ms debounce; a fixed short sleep misses the request.",
        },
    },
    {
        "id": "http500-null-deref",
        "title": "500 Server Error then Null Dereference",
        "category": "http-error",
        "html": _page(
            "500 then Null Deref",
            "<button id='go'>Trigger Broken Flow</button><div id='out'>idle</div>",
            "setInterval(()=>{fetch('/api/background-ping').catch(()=>{});},400);"
            "document.getElementById('go').onclick=async()=>{"
            "const res=await fetch('/api/failing-endpoint');const data=await res.json();"
            "document.getElementById('out').textContent=data.nonExistent.subField;};",
        ),
        "endpoints": {
            "/api/failing-endpoint": {"status": 500, "headers": {"Content-Type": "application/json"}, "body": '{"error":"Internal Error","code":500}'},
            "/api/background-ping": {"status": 200, "headers": {"Content-Type": "application/json"}, "body": '{"ping":true}'},
        },
        "ground_truth": {
            "action": {"type": "click", "target": "#go"},
            "triggered_request": "/api/failing-endpoint",
            "background_requests": ["/api/background-ping"],
            "exception_signature": "Cannot read properties of undefined",
            "expected_link": "weak",
            "failing_status": 500,
            "note": "Canonical case: failing fetch is attributed; background ping must stay temporal.",
        },
    },
    {
        "id": "iframe-worker-error",
        "title": "Iframe/Worker Security Exception",
        "category": "frame",
        "html": _page(
            "Iframe Worker Error",
            "<button id='go'>Read frame</button><iframe id='fr' src='/frame'></iframe><div id='out'>idle</div>",
            "document.getElementById('go').onclick=()=>{"
            "try{const d=document.getElementById('fr').contentWindow.document;"
            "document.getElementById('out').textContent=d.title||'same';}"
            "catch(e){document.getElementById('out').textContent='frame-error:'+e.name;}};",
        ),
        "endpoints": {
            "/frame": {"status": 200, "headers": {"Content-Type": "text/html"}, "body": "<title>child</title>child frame"},
        },
        "ground_truth": {
            "action": {"type": "click", "target": "#go"},
            "triggered_request": None,
            "background_requests": [],
            "exception_signature": None,
            "expected_link": "none",
            "failing_status": None,
            "note": "Frame access must be routed to the child session; top-frame-only snapshot misses it.",
        },
    },
    {
        "id": "hydration-mismatch",
        "title": "Hydration Mismatch",
        "category": "dom",
        "html": _page(
            "Hydration Mismatch",
            "<div id='app'><span id='srv'>server:0</span></div><button id='go'>Hydrate</button>",
            "document.getElementById('go').onclick=()=>{"
            "const el=document.getElementById('srv');"
            "if(el.textContent!=='client:0'){el.textContent='client-mismatch';}"
            "fetch('/api/hydration-state');};",
        ),
        "endpoints": {
            "/api/hydration-state": {"status": 200, "headers": {"Content-Type": "application/json"}, "body": '{"v":0}'},
        },
        "ground_truth": {
            "action": {"type": "click", "target": "#go"},
            "triggered_request": "/api/hydration-state",
            "background_requests": [],
            "exception_signature": None,
            "expected_link": "temporal",
            "failing_status": None,
            "note": "DOM diff after action must show server->client text change.",
        },
    },
    {
        "id": "broken-redirect-chain",
        "title": "Broken Redirect Chain",
        "category": "redirect",
        "html": _page(
            "Broken Redirect Chain",
            "<button id='go'>Follow</button><div id='out'>idle</div>",
            "document.getElementById('go').onclick=async()=>{"
            "const r=await fetch('/api/redirect-start');"
            "document.getElementById('out').textContent=String(r.status);};",
        ),
        "endpoints": {
            "/api/redirect-start": {"status": 302, "headers": {"Location": "/api/redirect-mid"}, "body": ""},
            "/api/redirect-mid": {"status": 302, "headers": {"Location": "/api/redirect-broken"}, "body": ""},
            "/api/redirect-broken": {"status": 404, "headers": {"Content-Type": "application/json"}, "body": '{"error":"gone"}'},
        },
        "ground_truth": {
            "action": {"type": "click", "target": "#go"},
            "triggered_request": "/api/redirect-start",
            "background_requests": [],
            "exception_signature": None,
            "expected_link": "temporal",
            "failing_status": 404,
            "note": "Redirect hops are frozen entries; final 404 must not overwrite the chain.",
        },
    },
    {
        "id": "websocket-disconnect",
        "title": "Intermittent WebSocket Disconnect",
        "category": "websocket",
        "html": _page(
            "WebSocket Disconnect",
            "<button id='go'>Connect</button><div id='out'>idle</div>",
            "document.getElementById('go').onclick=()=>{"
            "fetch('/api/ws-info');document.getElementById('out').textContent='ws-attempted';};",
        ),
        "endpoints": {
            "/api/ws-info": {"status": 200, "headers": {"Content-Type": "application/json"}, "body": '{"ws":"loopback-only","note":"WS upgrade handled in live-browser runs"}'},
        },
        "ground_truth": {
            "action": {"type": "click", "target": "#go"},
            "triggered_request": "/api/ws-info",
            "background_requests": [],
            "exception_signature": None,
            "expected_link": "temporal",
            "failing_status": None,
            "note": "Chrome-free tier asserts the info endpoint; live tier asserts close-frame capture.",
        },
    },
    {
        "id": "cache-stale-headers",
        "title": "Cache Poisoning / Stale Headers",
        "category": "cache",
        "html": _page(
            "Stale Cache Headers",
            "<button id='go'>Load cached</button><div id='out'>idle</div>",
            "document.getElementById('go').onclick=async()=>{"
            "const r=await fetch('/api/cached');const j=await r.json();"
            "document.getElementById('out').textContent=j.v;};",
        ),
        "endpoints": {
            "/api/cached": {"status": 200, "headers": {"Content-Type": "application/json", "Cache-Control": "public, max-age=31536000, immutable", "ETag": '"stale-v1"'}, "body": '{"v":"stale-v1"}'},
        },
        "ground_truth": {
            "action": {"type": "click", "target": "#go"},
            "triggered_request": "/api/cached",
            "background_requests": [],
            "exception_signature": None,
            "expected_link": "temporal",
            "failing_status": None,
            "note": "Immutable caching on a mutable payload is the stale-header signal.",
        },
    },
    {
        "id": "memory-leak-navigation",
        "title": "Memory Leak under Rapid Navigation",
        "category": "memory",
        "html": _page(
            "Memory Leak Navigation",
            "<button id='go'>Allocate</button><div id='out'>0</div>",
            "window.__leak=window.__leak||[];let n=0;"
            "document.getElementById('go').onclick=()=>{"
            "for(let i=0;i<1000;i++){window.__leak.push(new Array(1024).fill(i));}"
            "n++;document.getElementById('out').textContent=String(n);fetch('/api/ping');};",
        ),
        "endpoints": {
            "/api/ping": {"status": 200, "headers": {"Content-Type": "application/json"}, "body": '{"ok":true}'},
        },
        "ground_truth": {
            "action": {"type": "click", "target": "#go"},
            "triggered_request": "/api/ping",
            "background_requests": [],
            "exception_signature": None,
            "expected_link": "temporal",
            "failing_status": None,
            "note": "Repeated clicks grow retained arrays; heap snapshot delta is the live-tier signal.",
        },
    },
]

CASES_BY_ID: Dict[str, Case] = {c["id"]: c for c in CASES}


def get_case(case_id: str) -> Case:
    return CASES_BY_ID[case_id]


def all_case_ids() -> List[str]:
    return [c["id"] for c in CASES]
