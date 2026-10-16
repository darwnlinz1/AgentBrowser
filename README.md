# AgentBrowser: Autonomous Agent Browser Debugging Platform

An agent-native browser automation, deep inspection, HTTP/HTTPS debugging, causal action tracing, and verification platform designed for AI agents.

## Architecture

- **`agent_browser/`**: Core platform engine
  - `cdp.py`: Asynchronous Chrome DevTools Protocol multiplexer and session router
  - `dom.py`: Semantic accessibility tree extractor, selector resolver, and live DOM mutator
  - `network.py`: Full packet recorder, CDP Fetch interceptor, request replay, diffing, and AI-compact summary
  - `runtime.py`: Isolated JavaScript execution and uncaught exception tracer
  - `tracer.py`: Causal action tracer correlating clicks/inputs to network & DOM mutations
  - `diagnostics.py`: Automated root-cause analyzer and bug reproduction script generator
  - `storage.py`: Cookies, LocalStorage, and SessionStorage manager
  - `user_tracker.py`: Real-time human interaction capture (clicks, inputs, coordinates)
  - `focus.py`: Selective region observation with subtree delta hashing
  - `source_inspector.py`: Script harvester, initiator call stack resolver, AST/crypto scanner, early plaintext hooking
  - `proxy_engine.py`: Embedded local MITM proxy (mitmproxy engine) for universal browser (Firefox/Safari) & OpenBullet capture
  - `server.py`: Persistent FastAPI/Uvicorn daemon preserving browser sessions
  - `cli.py`: Agent-friendly CLI with structured JSON output
- **`.agents/skills/browser/SKILL.md`**: Official Antigravity `/browser` skill specification
- **`test_suite.py`**: Automated end-to-end integration verification suite
- **`test_cli.py`**: CLI interaction verification test
- **`test_edge_cases.py`**: Failure mode and edge case verification test
- **`test_user_tracking.py`**: User tracking and focus pinning verification test
- **`test_crypto_reverse.py`**: AI reverse engineering, script extraction & crypto verification test
- **`test_proxy_interception.py`**: Universal local MITM proxy verification test

## Quick Start

```powershell
# Start browser daemon with specific profile (e.g. dev1, debug_session2)
python -m agent_browser start --profile dev1 --headless

# List all browser profiles
python -m agent_browser profiles

# Navigate to URL
python -m agent_browser open "https://example.com"

# View token-efficient semantic snapshot
python -m agent_browser view

# Dump full page HTML source code
python -m agent_browser page-source

# Dump full site sources (HTML, scripts, sourcemaps) to local directory
python -m agent_browser dump-sources --output-dir dump_dir

# View AI-optimized compact network summary (smart filter ignores image/gif/font noise)
python -m agent_browser ai-traffic

# List all scripts loaded on the page
python -m agent_browser scripts

# Read full script source by ID
python -m agent_browser script-source <SCRIPT_ID>

# Scan scripts for encryption, signatures, and API callers
python -m agent_browser find-crypto

# Retrieve plaintext captured before encryption via early hooks
python -m agent_browser crypto-logs

# Find exact JavaScript source code & call stack for an API request
python -m agent_browser caller <REQUEST_ID>

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
python test_multiprofile_filter_sources.py
python test_edge_cases.py
python test_user_tracking.py
python test_crypto_reverse.py
python test_proxy_interception.py
```

Chrome-free unit/regression suites (no browser required):

```powershell
python -m pytest test_evidence_and_isolation.py test_phase0_hardening.py test_fault_corpus.py test_phase1_causal.py test_phase2_agentnative.py test_phase3_repro.py test_phaseP_hypercore.py test_benchmark_harness.py test_cycle*.py -q
```

Live harnesses (local loopback fixtures only, ephemeral ports):

```powershell
python -m fixtures.run_live_corpus          # 10-archetype attribution scoring (P/R, false-attributed, dropped)
python -m fixtures.run_daemon_smoke         # hardened daemon + CLI contract (token, Host/Origin, attribution)
python -m fixtures.run_intervention_live    # counterfactual A/B promotion to 'interventional'
python -m fixtures.run_journal_minimize_live # journal -> CDD minimization over hermetic replay
python -m benchmarks.run_benchmarks --live  # footprint / codec / token benchmarks (competitors only when installed)
```

## Platform notes

- Install: `pip install -r requirements.txt` (extras: `[proxy]`, `[fast]`, `[mcp]`, `[dev]` in `pyproject.toml`).
- The `view` command returns the budgeted accessibility-tree action graph by default; `AGENTBROWSER_VIEW=legacy` restores the older injected-JS view.
- The daemon (127.0.0.1) requires a per-daemon bearer token, which the CLI reads automatically; secrets are redacted on every egress route unless `include_secrets` is explicitly requested, and write-method request replays require `--allow-write`.
- On Windows the browser can run over `--remote-debugging-pipe` with no TCP port (`transport="pipe-windows"`).
- Roadmap and evaluation: `docs/engineering/BREAKTHROUGH_ROADMAP.md`, `docs/engineering/PROJECT_EVALUATION.md`, `docs/engineering/AUTONOMOUS_PROGRESS.md`.
