"""
Standalone reproduction synthesis (browser-free artifacts).

Currently emits a ``curl`` pipeline for network-reproducible bugs. Design
rules from the Track 10 research:
- Secrets never appear inline: Cookie/Authorization become environment
  placeholders (``AB_COOKIE`` / ``AB_AUTHORIZATION``) that the operator
  exports locally. Nothing secret is written into the artifact by default.
- Write methods are refused unless the caller explicitly opts in
  (``allow_write=True``); a captured POST must never become a one-click
  live write by accident.
- The artifact targets an origin the operator supplies/authorizes; the
  recorded URL is embedded as captured, and the caller is responsible for
  pointing it at local/staging.
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Dict, List, Optional

from agent_browser.evidence import url_template

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SECRET_HEADERS = {
    "cookie": "AB_COOKIE",
    "authorization": "AB_AUTHORIZATION",
    "proxy-authorization": "AB_PROXY_AUTHORIZATION",
    "x-api-key": "AB_API_KEY",
}


def _spec_literal(spec: Dict[str, Any]) -> str:
    """One JSON document, double-encoded so the literal is safe in Py and JS."""
    return json.dumps(json.dumps(spec, ensure_ascii=True), ensure_ascii=True)


def build_python_repro(url: str, target: str, *, action: str = "click",
                       expected_exception: Optional[str] = None,
                       expected_status: Optional[int] = None) -> str:
    """
    Standalone (repo-local) Python reproduction using AgentBrowserController.

    All recorded data travels in one embedded JSON literal -- a URL or
    selector can never become code. Exit codes follow git-bisect convention:
    0 = expectation met, 1 = not met.
    """
    spec = {"url": url, "target": target, "action": action,
            "expected_exception": expected_exception, "expected_status": expected_status}
    lines = [
        "#!/usr/bin/env python3",
        '"""Standalone AgentBrowser reproduction. Usage: repro.py [--expect bug|fixed]"""',
        "import argparse, asyncio, json, os, sys",
        "# Locate the agent_browser package: $AGENTBROWSER_HOME, this script's",
        "# directory, or the current directory (repo-local artifact).",
        "for _p in (os.environ.get('AGENTBROWSER_HOME'), os.path.dirname(os.path.abspath(__file__)), os.getcwd()):",
        "    if _p and os.path.isdir(os.path.join(_p, 'agent_browser')):",
        "        sys.path.insert(0, _p)",
        "        break",
        "from agent_browser.agent_api import AgentBrowserController",
        "",
        f"SPEC = json.loads({_spec_literal(spec)})",
        "",
        "async def _run():",
        "    from agent_browser.session_registry import find_free_port",
        "    c = AgentBrowserController(port=find_free_port(), headless=True, profile='ab-repro')",
        "    await c.start()",
        "    try:",
        "        await c.navigate(SPEC['url'])",
        "        trace = await c.trace_action(SPEC['action'], target=SPEC['target'])",
        "        exc_ok = SPEC['expected_exception'] is None or any(",
        "            SPEC['expected_exception'] in str(e.get('description') or e.get('text') or '')",
        "            for e in trace['runtime']['exceptions'])",
        "        status_ok = SPEC['expected_status'] is None or any(",
        "            r.get('status') == SPEC['expected_status'] for r in trace['network']['requests'])",
        "        return bool(trace['runtime']['exceptions'] or trace['network']['failing_requests_count']) and exc_ok and status_ok",
        "    finally:",
        "        await c.stop()",
        "",
        "def main():",
        "    ap = argparse.ArgumentParser()",
        "    ap.add_argument('--expect', choices=('bug', 'fixed'), default='bug')",
        "    a = ap.parse_args()",
        "    reproduced = asyncio.run(_run())",
        "    ok = reproduced if a.expect == 'bug' else not reproduced",
        "    print(json.dumps({'reproduced': reproduced, 'expect': a.expect}))",
        "    sys.exit(0 if ok else 1)",
        "",
        "if __name__ == '__main__':",
        "    main()",
        "",
    ]
    return "\n".join(lines)


_NODE_TEMPLATE = r"""#!/usr/bin/env node
'use strict';
/* Standalone zero-dependency AgentBrowser reproduction (CDP over --remote-debugging-pipe).
   Usage: node repro.mjs [--expect=fixed]  (default --expect=bug; exit 0 = expectation met) */
const { spawn } = require('node:child_process');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const SPEC = JSON.parse(__SPEC_LITERAL__);
const CHROME_CANDIDATES = [process.env.CHROME,
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome', '/usr/bin/chromium'].filter(Boolean);

class CDP {
  constructor(out, inp) {
    this.out = out; this.id = 0; this.pending = new Map(); this.handlers = new Set();
    let buf = Buffer.alloc(0);
    inp.on('data', (chunk) => {
      buf = Buffer.concat([buf, chunk]);
      let i;
      while ((i = buf.indexOf(0)) !== -1) {
        const m = JSON.parse(buf.subarray(0, i).toString('utf8'));
        buf = buf.subarray(i + 1);
        if (m.id !== undefined) {
          const p = this.pending.get(m.id);
          if (p) { this.pending.delete(m.id); m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result || {}); }
        } else for (const h of this.handlers) h(m);
      }
    });
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id, msg = { id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    this.out.write(JSON.stringify(msg) + '\0');
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }
}

async function main() {
  const expect = process.argv.includes('--expect=fixed') ? 'fixed' : 'bug';
  const chrome = CHROME_CANDIDATES.find((p) => fs.existsSync(p));
  if (!chrome) process.exit(125);
  const prof = fs.mkdtempSync(path.join(os.tmpdir(), 'ab-repro-'));
  const proc = spawn(chrome, ['--remote-debugging-pipe', '--headless=new', '--disable-gpu',
    '--no-first-run', '--no-default-browser-check', `--user-data-dir=${prof}`, 'about:blank'],
    { stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'] });
  const cdp = new CDP(proc.stdio[3], proc.stdio[4]);
  const exceptions = [], statuses = [];
  try {
    const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
    const { sessionId: sid } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
    let loaded = false;
    cdp.handlers.add((m) => {
      if (m.sessionId !== sid) return;
      if (m.method === 'Page.loadEventFired') loaded = true;
      if (m.method === 'Runtime.exceptionThrown') {
        const d = m.params.exceptionDetails || {};
        exceptions.push((d.exception && d.exception.description) || d.text || '');
      }
      if (m.method === 'Network.responseReceived') statuses.push(m.params.response.status);
    });
    await cdp.send('Page.enable', {}, sid);
    await cdp.send('Runtime.enable', {}, sid);
    await cdp.send('Network.enable', {}, sid);
    await cdp.send('Page.navigate', { url: SPEC.url }, sid);
    for (let i = 0; i < 100 && !loaded; i++) await new Promise((r) => setTimeout(r, 50));
    await cdp.send('Runtime.evaluate', {
      expression: `(() => { const el = document.querySelector(${JSON.stringify(SPEC.target)});
        if (!el) return false; el.scrollIntoView(); el.dispatchEvent(new MouseEvent('click', {bubbles: true})); return true; })()`,
      returnByValue: true,
    }, sid);
    await new Promise((r) => setTimeout(r, 1200));
    const excOk = SPEC.expected_exception == null || exceptions.some((e) => e.includes(SPEC.expected_exception));
    const statusOk = SPEC.expected_status == null || statuses.includes(SPEC.expected_status);
    const reproduced = exceptions.length > 0 && excOk && statusOk;
    console.log(JSON.stringify({ reproduced, exceptions: exceptions.length, statuses, expect }));
    process.exitCode = (expect === 'bug' ? reproduced : !reproduced) ? 0 : 1;
  } finally {
    try { proc.kill(); } catch (_) {}
    // The profile may still be locked while Chrome exits; cleanup must
    // never throw, or it would mask the real exit code with 125.
    try { fs.rmSync(prof, { recursive: true, force: true, maxRetries: 3, retryDelay: 200 }); } catch (_) {}
  }
}
main().catch(() => process.exit(125));
"""


def build_node_repro(url: str, target: str, *, expected_exception: Optional[str] = None,
                     expected_status: Optional[int] = None) -> str:
    """
    Zero-dependency Node reproduction (CDP over ``--remote-debugging-pipe``,
    no port, no npm packages). Same embedded-JSON-spec safety rule as the
    Python form; exit codes follow git-bisect convention (125 = cannot run).
    """
    spec = {"url": url, "target": target,
            "expected_exception": expected_exception, "expected_status": expected_status}
    return _NODE_TEMPLATE.replace("__SPEC_LITERAL__", _spec_literal(spec))


def build_curl(entry: Dict[str, Any], *, include_secrets: bool = False, allow_write: bool = False) -> Dict[str, Any]:
    """
    Build a curl reproduction for one captured request entry.

    Returns {command_posix, command_windows, env_required, method, url_template}
    or {refused: True, reason} for a write without explicit opt-in.
    """
    method = (entry.get("method") or "GET").upper()
    url = entry.get("url") or ""
    if method not in SAFE_METHODS and not allow_write:
        return {
            "refused": True,
            "reason": f"{method} is a write method; pass allow_write=True to synthesize its curl reproduction",
            "method": method,
            "url_template": url_template(url),
        }

    headers = dict(entry.get("headers") or {})
    env_required: List[str] = []
    header_args: List[str] = []
    for name, value in headers.items():
        lname = str(name).lower()
        if lname.startswith(":") or lname == "content-length":
            continue
        if lname in _SECRET_HEADERS and not include_secrets:
            env = _SECRET_HEADERS[lname]
            env_required.append(env)
            header_args.append((name, f"${{{env}}}"))
        else:
            header_args.append((name, str(value)))

    body = entry.get("request_body") or ""
    posix_parts = ["curl", "-sS", "-i", "-X", method]
    win_parts = ["curl.exe", "-sS", "-i", "-X", method]
    for name, value in header_args:
        posix_parts += ["-H", f"{name}: {value}"]
        win_parts += ["-H", f"{name}: {value}"]
    if body and method not in ("GET", "HEAD"):
        posix_parts += ["--data-binary", body]
        win_parts += ["--data-binary", body]
    posix_parts.append(url)
    win_parts.append(url)

    command_posix = " ".join(shlex.quote(p) if i else p for i, p in enumerate(posix_parts))
    command_windows = " ".join(
        p if i == 0 else '"' + p.replace('"', '\\"') + '"'
        for i, p in enumerate(win_parts)
    )
    return {
        "refused": False,
        "method": method,
        "url": url,
        "url_template": url_template(url),
        "command_posix": command_posix,
        "command_windows": command_windows,
        "env_required": sorted(set(env_required)),
        "note": (
            "Export the listed env vars locally before running; they are never "
            "embedded. Point the URL at an authorized local/staging origin."
        ),
    }
