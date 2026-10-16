---
name: browser
description: Autonomous Agent Browser Debugging, Inspection, HTTP Traffic Interception, Causal Action Tracing, and Verification Platform. Use this skill whenever inspecting web pages, reading and mutating DOM elements, tracing user interactions to downstream effects, debugging JavaScript errors, intercepting/replaying HTTP/WebSocket traffic, or diagnosing web application issues.
---

# AgentBrowser: Autonomous Agent Browser Debugging Platform

The **AgentBrowser** platform transforms a web browser into a deep, agent-native debugging environment. Instead of raw automation, it allows the agent to execute the complete engineering cycle:

```
OBSERVE → INSPECT → TRACE → MODIFY → REPLAY → DEBUG → VERIFY
```

## Quick Reference: CLI Commands

All commands can be executed directly via terminal using:
`python -m agent_browser <command> [options]`

| Command | Usage | Description |
|---|---|---|
| `start` | `python -m agent_browser start [--profile <name>] [--headless]` | Spawns daemon & browser with isolated profile directory |
| `profiles`| `python -m agent_browser profiles` | Lists all browser profiles and active profile status |
| `status` | `python -m agent_browser status` | Checks browser and target session status |
| `open` | `python -m agent_browser open <url>` | Navigates active tab to target URL |
| `page-source`| `python -m agent_browser page-source [--output file.html]` | Dumps full HTML source code of active document |
| `dump-sources`| `python -m agent_browser dump-sources [--output-dir <dir>]` | Archives HTML, all scripts, and sourcemaps to disk/bundle |
| `view` | `python -m agent_browser view` | Outputs token-efficient semantic view with `[#ref]` tags |
| `snapshot`| `python -m agent_browser snapshot` | Returns full machine-readable JSON DOM tree |
| `click` | `python -m agent_browser click <selector_or_#ref>` | Clicks element and outputs correlated Action Trace Report |
| `type` | `python -m agent_browser type <selector_or_#ref> "<text>"` | Types into input with full causal tracing |
| `trace` | `python -m agent_browser trace <action> [--target <t>]` | Traces any action (`click`, `type`, `navigate`, `eval`, `hover`) |
| `eval` | `python -m agent_browser eval "<javascript>"` | Evaluates JavaScript expression in page context |
| `traffic` | `python -m agent_browser traffic [--search <q>] [--status <s>]` | Searches captured HTTP/HTTPS requests & responses |
| `replay` | `python -m agent_browser replay <requestId> [--mutate-header K=V]` | Replays request and diffs response against baseline |
| `intercept`| `python -m agent_browser intercept add <pattern> --mock-body <json>` | Mocks or modifies matching network requests |
| `diagnose`| `python -m agent_browser diagnose` | Runs automated root-cause error & failure analysis |
| `cookies` | `python -m agent_browser cookies [--set "k=v;domain=..."]` | Inspects or sets cookies |
| `storage` | `python -m agent_browser storage [--type local|session]` | Inspects or sets LocalStorage / SessionStorage |
| `mutate-dom`| `python -m agent_browser mutate-dom <sel> <action> [--kwargs <json>]` | Modifies attributes, styles, or HTML of live elements |
| `pin` | `python -m agent_browser pin <selector> [--alias <name>]` | Pins a specific element/region of interest to watch |
| `unpin` | `python -m agent_browser unpin <alias_or_selector>` | Unpins a previously watched region |
| `focus-view`| `python -m agent_browser focus-view` | Inspects only pinned elements and their recent deltas |
| `user-events`| `python -m agent_browser user-events [--limit N]` | Retrieves human user actions (pixel, selector, timestamp) |
| `last-action`| `python -m agent_browser last-action` | Gets the single most recent user action performed on page |
| `watch` | `python -m agent_browser watch` | Directed dashboard combining pinned regions + last action |
| `ai-traffic`| `python -m agent_browser ai-traffic [--limit N]` | AI-optimized token-lean network summary with API payloads |
| `scripts` | `python -m agent_browser scripts [--no-inline]` | Lists all scripts loaded into V8 (inline & external) |
| `reconstruct` | `python -m agent_browser reconstruct` | Rebuilds original sources from source maps + API endpoint inventory |
| `script-source`| `python -m agent_browser script-source <script_id>` | Fetches full raw JavaScript source code |
| `find-crypto`| `python -m agent_browser find-crypto [--script-id <id>]` | AST/Regex scanner for AES, RSA, signatures, and API calls |
| `crypto-logs`| `python -m agent_browser crypto-logs` | Plaintext captured before encryption via early document hooks |
| `caller` | `python -m agent_browser caller <requestId>` | Maps HTTP request to exact JS file, line, and call stack |
| `proxy-traffic`| `python -m agent_browser proxy-traffic [--query <q>]` | Captures universal traffic from Firefox, Safari, OpenBullet |
| `screenshot`| `python -m agent_browser screenshot [--output file.png]` | Captures page viewport or full scrollable screenshot |
| `tabs` | `python -m agent_browser tabs` | Lists attached page tabs (multi-tab) |
| `activate-tab`| `python -m agent_browser activate-tab <session_id>` | Makes a page tab the active session |
| `mcp` | `python -m agent_browser mcp --list-tools` | Lists the MCP tool catalog (stdio server: `python -m agent_browser.mcp_server`) |
| `perf` | `python -m agent_browser perf` | Web-vitals snapshot (LCP/CLS/long-tasks, graded) + renderer metrics |
| `debug-loop` | `python -m agent_browser debug-loop <url> --target <#ref>` | Runs an OBSERVE..VERIFY episode with evidence gates |
| `dialogs` / `dialog-policy` | `python -m agent_browser dialogs` / `dialog-policy accept|dismiss` | JavaScript dialog log + handling policy (default dismiss) |
| `downloads` | `python -m agent_browser downloads` | Downloads captured in this session (confined directory) |
| `handoff` | `python -m agent_browser handoff start "reason"` / `handoff complete "summary"` | Human takeover: agent actions refused until completed |
| `stop` | `python -m agent_browser stop` | Gracefully closes browser session and background daemon |

**Platform notes (current implementation):**
- `view` now returns the budgeted **accessibility-tree** action graph by default (node-identity `[#ref]`s, no page pollution; `AGENTBROWSER_VIEW=legacy` restores the injected-JS view). Clicks by `#ref` route to whichever snapshot minted them.
- Causal traces carry evidence grades: `temporal` (co-occurrence) < `weak`/`supported` < **`attributed`** (initiator stack contains the action's stamped sourceURL) < **`interventional`** (A/B experiment). Settling is event-driven with a stamped probe (opt-in strict idle probe: `AGENTBROWSER_STRICT_SETTLE=1`).
- The daemon requires a loopback bearer token (the CLI reads it automatically), validates Host/Origin, redacts secrets on egress (`include_secrets` opts in), and writes files only under `agent_output/`. Write-method replays are refused without `--allow-write`.
- Transports: WebSocket (default) or, on Windows, `--remote-debugging-pipe` with no TCP port (`transport="pipe-windows"` / `AGENTBROWSER_TRANSPORT=pipe-windows`).
- Verification harnesses: `python -m fixtures.run_live_corpus`, `python -m fixtures.run_daemon_smoke`, `python -m fixtures.run_intervention_live`, `python -m fixtures.run_journal_minimize_live`, `python -m benchmarks.run_benchmarks --live`.

---

## 1. Agent-Native Workflow Runbooks

### Runbook A: Semantic Navigation & Interactive Exploration
When navigating to a new website:
1. Open the URL:
   ```powershell
   python -m agent_browser open "https://example.com"
   ```
2. Get the token-efficient view:
   ```powershell
   python -m agent_browser view
   ```
   *The output will show clean interactive elements tagged with `[#1]`, `[#2]`, etc.*
3. Perform an action using the ref number:
   ```powershell
   python -m agent_browser click "#2"
   ```

### Runbook B: Causal Action Tracing (Debugging Side-Effects)
When a button click or form submission fails or triggers unexpected behaviors:
1. Execute the action through the tracer:
   ```powershell
   python -m agent_browser click "#submit-btn"
   ```
2. Inspect the resulting **Action Trace Report** JSON:
   - `navigation`: whether the URL changed or redirected.
   - `dom_changes`: count and sample of DOM nodes added/removed or attribute changes.
   - `network`: exact list of HTTP requests fired by the click, including status codes and latency.
   - `runtime`: uncaught exceptions or `console.error` logs triggered by the click.
   - `diagnostics`: automatic root-cause correlation explaining why the action succeeded or failed.

### Runbook C: Network Interception & API Tampering (Burp Suite for Agents)
When testing edge cases, mocking failing backends, or bypassing client-side constraints:
1. Add an interception rule to mock an API response:
   ```powershell
   python -m agent_browser intercept add "/api/v1/user" --mock-status 200 --mock-body '{"role":"admin","credits":9999}'
   ```
2. Trigger the page action:
   ```powershell
   python -m agent_browser click "#refresh-profile"
   ```
3. Clear interception rules when testing is complete:
   ```powershell
   python -m agent_browser intercept clear
   ```

### Runbook D: Request Replay & Parameter Diffing
When reproducing an API bug or investigating authorization:
1. Search traffic for the relevant request:
   ```powershell
   python -m agent_browser traffic --search "/v1/order"
   ```
2. Replay the captured request with mutated headers or payload:
   ```powershell
   python -m agent_browser replay "<request_id>" --mutate-header "Authorization=Bearer ${AB_AUTHORIZATION}"  # authorized targets only; writes need --allow-write
   ```
3. Review the side-by-side unified diff comparing original response vs replayed response.

### Runbook E: Automated Root-Cause Diagnostics
When a page is broken or misbehaving:
1. Run diagnostic scan:
   ```powershell
   python -m agent_browser diagnose
   ```
2. The engine automatically links:
   - Failing network requests (e.g. 500 Internal Error or 403 Forbidden)
   - Downstream JavaScript exceptions (`TypeError: Cannot read properties of undefined`)
   - CORS or CSP policy violations
   - Root-cause attribution and recommended remedies.

### Runbook F: Tracking Human Actions in Real-Time
When the user is interacting on the site and the Agent needs to observe what was done:
1. Inspect the last user action:
   ```powershell
   python -m agent_browser last-action
   ```
2. Retrieve the stream of recent interactions (pixel coords, exact CSS selector, event type):
   ```powershell
   python -m agent_browser user-events --limit 10
   ```

### Runbook G: Directed Focus Pinning (Zero Token Waste)
Instead of forcing the Agent to read a 50,000-token HTML document, pin specific regions:
1. Pin the area under investigation (e.g. cart or checkout form):
   ```powershell
   python -m agent_browser pin "#checkout-container" --alias "checkout"
   ```
2. Read only the focused region and detect recent mutations:
   ```powershell
   python -m agent_browser focus-view
   ```
### Runbook H: AI Reverse-Engineering & Script Decryption
When an API has encrypted payload/signatures or obfuscated logic:
1. Examine the token-efficient network summary to find the encrypted endpoint:
   ```powershell
   python -m agent_browser ai-traffic
   ```
2. Inspect the pre-encryption logs (captured by early hooks on `window.fetch`, `XHR`, `JSON.stringify`, `crypto.subtle`):
   ```powershell
   python -m agent_browser crypto-logs
   ```
3. Locate the exact JavaScript caller and call stack that initiated the request:
   ```powershell
   python -m agent_browser caller <REQUEST_ID>
   ```
4. Harvest all scripts and scan for cryptographic algorithms (AES, RSA, HMAC, SHA):
   ```powershell
   python -m agent_browser find-crypto
   ```
5. Fetch the complete source code of the cryptographic script module for full analysis:
   ```powershell
   python -m agent_browser script-source <SCRIPT_ID>
   ```

---


## 2. Programmatic Python API

For direct in-script integration:
```python
import asyncio
from agent_browser.agent_api import AgentBrowserController

async def main():
    browser = AgentBrowserController(headless=True)
    await browser.start()
    try:
        await browser.navigate("https://example.com")
        view = await browser.get_agent_view()
        print(view)

        # Causal trace
        report = await browser.trace_action("click", target="a")
        print(report["diagnostics"])
    finally:
        await browser.stop()

if __name__ == "__main__":
    asyncio.run(main())
```
