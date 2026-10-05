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
| `start` | `python -m agent_browser start [--headless]` | Spawns background daemon and Chrome browser |
| `status` | `python -m agent_browser status` | Checks browser and target session status |
| `open` | `python -m agent_browser open <url>` | Navigates active tab to target URL |
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
| `screenshot`| `python -m agent_browser screenshot [--output file.png]` | Captures page viewport or full scrollable screenshot |
| `stop` | `python -m agent_browser stop` | Gracefully closes browser session and background daemon |

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
   python -m agent_browser replay "<request_id>" --mutate-header "Authorization=Bearer forged_token"
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
