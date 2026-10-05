# AgentBrowser: Autonomous Agent Browser Debugging Platform

An agent-native browser automation, deep inspection, HTTP/HTTPS debugging, causal action tracing, and verification platform designed for AI agents.

## Architecture

- **`agent_browser/`**: Core platform engine
  - `cdp.py`: Asynchronous Chrome DevTools Protocol multiplexer and session router
  - `dom.py`: Semantic accessibility tree extractor, selector resolver, and live DOM mutator
  - `network.py`: Full packet recorder, CDP Fetch interceptor, request replay and diffing
  - `runtime.py`: Isolated JavaScript execution and uncaught exception tracer
  - `tracer.py`: Causal action tracer correlating clicks/inputs to network & DOM mutations
  - `diagnostics.py`: Automated root-cause analyzer and bug reproduction script generator
  - `storage.py`: Cookies, LocalStorage, and SessionStorage manager
  - `server.py`: Persistent FastAPI/Uvicorn daemon preserving browser sessions
  - `cli.py`: Agent-friendly CLI with structured JSON output
- **`.agents/skills/browser/SKILL.md`**: Official Antigravity `/browser` skill specification
- **`test_suite.py`**: Automated end-to-end integration verification suite
- **`test_cli.py`**: CLI interaction verification test
- **`test_edge_cases.py`**: Failure mode and edge case verification test

## Quick Start

```powershell
# Start browser daemon
python -m agent_browser start --headless

# Navigate to URL
python -m agent_browser open "https://example.com"

# View token-efficient semantic snapshot
python -m agent_browser view

# Trace click with causal downstream effects
python -m agent_browser click "#submit-btn"

# Run automated diagnostics
python -m agent_browser diagnose

# Stop daemon
python -m agent_browser stop
```

## Running Verification Tests

```powershell
python test_suite.py
python test_cli.py
python test_edge_cases.py
```
