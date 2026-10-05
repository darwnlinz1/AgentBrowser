# AgentBrowser - Autonomous Agent Browser Debugging Platform

A browser debugging and execution platform natively designed for AI Agents.

## Architectural Overview

```
                      +-----------------------------------+
                      |      AI Agent / Antigravity       |
                      +-----------------+-----------------+
                                        | (CLI / REST / Python SDK)
                                        v
                      +-----------------------------------+
                      |    agent_browser.server / CLI     |
                      +-----------------+-----------------+
                                        |
       +--------------------------------+--------------------------------+
       |                                |                                |
       v                                v                                v
+---------------+              +-----------------+              +----------------+
|  DOM Engine   |              |  Action Tracer  |              | Network Engine |
| - Snapshots   |              | - Causal Spans  |              | - Interception |
| - [#ref] Tags |              | - Downstream    |              | - Replay & Diff|
| - Live Mutate |              |   Correlations  |              | - WS & gRPC    |
+---------------+              +-----------------+              +----------------+
       |                                |                                |
       +--------------------------------+--------------------------------+
                                        |
                                        v
                      +-----------------------------------+
                      |    CDP Multiplexer & Session Mgr  |
                      +-----------------+-----------------+
                                        | (Chrome DevTools Protocol)
                                        v
                      +-----------------------------------+
                      |   Google Chrome / Microsoft Edge  |
                      |   (Real Headless / Headed Engine) |
                      +-----------------------------------+
```

## Core Modules

1. **`agent_browser.cdp`**: Async CDP Multiplexer with multi-session routing and process manager.
2. **`agent_browser.dom`**: Token-efficient accessibility tree parser, query engine, and live DOM mutator.
3. **`agent_browser.network`**: Full HTTP/HTTPS & WebSocket recorder, CDP `Fetch` interception engine, request replay, and diffing.
4. **`agent_browser.runtime`**: JavaScript execution environment with real-time console and uncaught exception capture.
5. **`agent_browser.tracer`**: Causal action tracer correlating user interactions with network calls, DOM mutations, and errors.
6. **`agent_browser.diagnostics`**: Automated root-cause analyzer linking network failures to JS exceptions and generating reproduction scripts.
7. **`agent_browser.storage`**: Cookie, LocalStorage, and SessionStorage inspector and mutator.
8. **`agent_browser.server`**: Persistent FastAPI/Uvicorn daemon server preserving browser state between tool executions.
9. **`agent_browser.cli`**: Agent-friendly CLI with structured JSON output and interactive command support.
10. **`agent_browser.agent_api`**: High-level programmatic Python SDK.

## CLI Usage

```powershell
# Start browser daemon
python -m agent_browser start --headless

# Navigate to page
python -m agent_browser open "http://127.0.0.1:8898"

# View token-efficient interactive elements
python -m agent_browser view

# Trace click on element #2 or selector #submit-btn
python -m agent_browser click "#btn-mutate"

# Search network traffic
python -m agent_browser traffic --search "/api"

# Replay request with mutated header
python -m agent_browser replay "<request_id>" --mutate-header "X-Debug=1"

# Add interception / mock rule
python -m agent_browser intercept add "/api/data" --mock-body '{"status":"ok"}'

# Run automated diagnostics
python -m agent_browser diagnose

# Inspect cookies & storage
python -m agent_browser cookies
python -m agent_browser storage --type local

# Capture screenshot
python -m agent_browser screenshot --output test.png

# Stop browser
python -m agent_browser stop
```
