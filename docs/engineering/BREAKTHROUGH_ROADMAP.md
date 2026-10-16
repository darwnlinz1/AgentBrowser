# AgentBrowser Breakthrough Roadmap (HYPER-BROWSER backlog)

This is the backlog the in-session Loop and the scheduled Automation consume.
One item per cycle: implement on a feature branch -> self-test (pytest +
seeded fault corpus) -> code review -> open/update PR + append to
`AUTONOMOUS_PROGRESS.md`, or revert and log why. Corpus gate metrics:
attribution precision/recall, false-supported rate (target 0), dropped-event
rate (target 0), replay fidelity, time-to-root-cause. See
`fixtures/fault_corpus.py` (10 archetypes) and `fixtures/metrics.py`.

## Legitimate-use boundary (applies to every item)

Debugging, QA, and authorized security testing on systems the operator owns or
is authorized to test. Never build: CAPTCHA solving, stealth/fingerprint
spoofing, credential stuffing, account checking, or attack orchestration.
Crypto/source instrumentation stays observe-only, consent-gated, and redacted.
The autonomous loop runs only against the local corpus — never live third-party
sites, never auto-replaying write requests at a live backend — and never
commits live-secret artifacts (fenced by `.gitignore`).

## North-star vectors (HYPER-BROWSER)

1. Extreme lightweight footprint (streaming, near-zero RAM/CPU).
2. Microsecond source & network inspection (in-memory, no disk round-trips).
3. Ultra-dense token efficiency (100k-token DOM -> 300-500 token action graph).
4. Native MCP + causal reverse-engineering (click -> JS stack -> network -> DOM).

## Phase 0 — Foundation & safety (DONE)

- `pyproject.toml` / `requirements.txt`; cross-platform `find_system_browser`.
- Daemon hardening (`agent_browser/security.py` + `server.py` middleware):
  loopback bearer token, Host/Origin validation, confined file-writing routes,
  `/eval` + `/mutate_dom` capability gate, token-guarded `/shutdown`.
- On-egress redaction (`evidence.redact_entry`) for traffic/cookies/storage/
  proxy routes with `include_secrets` opt-in; password/OTP/card values stripped
  from the DOM snapshot.
- Reliability: `Fetch.requestPaused` never dropped (drop counter surfaced),
  `on_drop` auto-reconnect wired to `AgentBrowserController.reconnect()`.
- Tests: `test_phase0_hardening.py`.

## Fault corpus (DONE)

`fixtures/fault_corpus.py` (10 archetypes), `fixtures/fixture_server.py`
(ephemeral port 0), `fixtures/metrics.py`, `test_fault_corpus.py`.

## Phase 1 — Causal-debugging core (unique edge)

| Item | Modules | Impact/Difficulty | Acceptance |
|---|---|---|---|
| Stack-stamped causal graph: tag actions `__agent_internal_action_<id>`, walk `initiator.stack.parent`, add `attributed`/`proven` confidence | `dom.py`, `tracer.py`, `evidence.py`, `network.py`, `runtime.py` | High / Med | Corpus case 4: failing fetch `attributed`, background ping stays `temporal` |
| Deterministic quiescence (zero-sleep settling): 4 vectors (microtask drain, in-flight network excl. WS/SSE/long-poll, DOM-quiet, lifecycle by loaderId) | `tracer.py`, `cdp.py` | High / Med | No fixed sleep; `settled_by` reported; corpus cases 2,3 pass without false-early |
| Fix `type_text` focus bug (`DOM.focus` / `Input.insertText`) | `dom.py` | High / Low | Typing lands in focused field on React-controlled inputs |
| Structured CORS/CSP via CDP `Audits` + `blockedReason`/`corsErrorStatus` | `diagnostics.py`, `agent_api.py`, `network.py` | Med-High / Low | Corpus case 1 classified without console-text matching |
| Trustworthy repro generator (stable locators + failure-signature oracle), exposed via CLI | `diagnostics.py`, `cli.py` | Med / Low | Generated script reproduces corpus case 4 and fails on signature only |

## Phase 2 — Multi-session + agent-native surface

| Item | Modules | Impact/Difficulty |
|---|---|---|
| Session registry (`session_id -> controller`) + ephemeral ports; `--page` routing | `server.py`, `agent_api.py`, `cdp.py`, `cli.py` | High / Med-High |
| MCP server: ~18 curated capability-tiered tools (Inspection/Mutation/Interception/Diagnostics), redacted typed outputs, explicit session/page handles; `eval`/`shutdown` never exposed | new `agent_browser/mcp_server.py`, `server.py`, `cli.py` | High / Med |
| Accessibility-tree node-identity refs (`Accessibility.getFullAXTree` + `DOMSnapshot`, `backendNodeId` table, isolated world; no `data-agent-ref` pollution); shadow DOM + cross-origin iframe + worker coverage; diff-first snapshots | `dom.py`, `agent_api.py`, `focus.py` | High / Med-High |
| Self-healing element fingerprints (role/name/test-id/text/box, similarity ranking, heal logged as evidence) | `dom.py`, `focus.py`, `evidence.py` | High / Med |
| Human handoff (`awaiting_human`, screencast, human-points-agent-sees) | `server.py`, `agent_api.py`, `user_tracker.py` | High / Med |

## Phase 3 — Reproduction & verification pipeline

| Item | Modules | Impact/Difficulty |
|---|---|---|
| In-browser + differential replay (`Network.replayXHR`, credentialed in-page `fetch`, HAR export) | `network.py`, new `replay.py` | High / Med |
| Response-stage interception + match-and-replace (dev-target gated, audit-logged) | `network.py` | High / Med |
| Interventional A/B confirmation (top `interventional` evidence rung) | `evidence.py`, `diagnostics.py`, `network.py`, `server.py` | High / Med |
| Hermetic replay -> flaky-aware delta-debugging minimizer (ddmin/ProbDD, k-of-n) -> regression synthesis (Python/Node/`curl` + failure-signature assertions) | new `replay.py`, `minimizer.py`, `diagnostics.py`, `evidence.py` | High / Med-High |
| OBSERVE..VERIFY state machine with evidence gates (repro pre-fix, test fail-pre/pass-post, causal edge disappears, stable k reruns) | new `loop_engine.py`, `diagnostics.py`, `tracer.py` | High / Med |

## Phase P — HYPER core (footprint + microsecond transport + token density)

| Item | Modules | Impact/Difficulty |
|---|---|---|
| Zero-copy CDP transport: `msgspec` decoding over `--remote-debugging-pipe`, loopback fallback, `uvloop`/`winloop` | `cdp.py`, new transport layer | High / High |
| Lockless ring buffer, lossless Fetch events, backpressure + drop accounting | `cdp.py`, `agent_api.py` | High / Med |
| Lightweight Chromium flag profiles (images off, renderer limits, memory pressure) keeping Audits/Network/Runtime/Debugger active | `cdp.py` (`BrowserProcessManager`) | Med-High / Low |
| Merkle subtree-hash AXTree diffing (100k -> 300-500 tokens, changed-subtree only) | `dom.py`, `focus.py` | High / Med |
| Streaming capture + noise gate + compact (<1k token) traffic serialization; WS/SSE/gRPC-Web decoding | `network.py`, `proxy_engine.py` | High / Med |
| In-memory source pipeline: `sourcesContent` tree rebuild, fast AST endpoint inventory, WASM disassembly (authorized-scope gated) | `source_inspector.py` | Med-High / Med |

## Benchmark harness

Same corpus/pages through Playwright/Puppeteer/Crawl4AI: RAM/session, cold
start, CDP event throughput, tokens-per-page, time-to-root-cause. Numbers gate
every cycle; a regression reverts the change.

## Quality gates & evaluation

Every change: self-tested (pytest + corpus) and code-reviewed (self-review plus
a Bugbot pass on the diff). After the first implemented batch: a project
evaluation (architecture health, benchmark numbers, scope/risk).

## Round-2 deep-dive fold-in (2026-10-15, backlog for next cycles)

Folded after the first batch shipped. Quick wins from these reports were applied immediately (websockets compression off, DevToolsActivePort ownership proof, Debugger.setSkipAllPauses, Page.setLifecycleEventsEnabled, stale data-agent-ref cleanup, write-gated replay, off-loop replay route, broader .gitignore). Remaining design items, in priority order:

1. Transport v2 (Track 1): msgspec Envelope with Raw params (zero-copy routing), synchronous inline critical handlers + per-session FIFO lanes with counted spill, body-fetcher pool + Network.configureDurableMessages, Windows --remote-debugging-io-pipes (Chrome 120+), narrow Fetch patterns + Chrome 155 AvoidCorsURLLoaderRestartOnRedirect flag, DevToolsActivePort readiness instead of 250ms HTTP polling (cold-start diet, top gap).
2. Quiescence v2 (Track 8): synchronous QuiescenceMonitor with stamped (activity_seq, doc_epoch) probes, request ledger with heartbeat/long-poll classification, loaderId-filtered wait_for_navigation, SPA route windows, type-action floor/strict timer mode.
3. AX view v2 (Track 4): budgeted keyframe view + revisioned deltas ONLY as a supplement (Playwright withdrew delta-only snapshots; explicit view always returns a full budgeted view), stale-ref fingerprint revalidation, DOMSnapshot layout join, scoped queryAXTree pin re-reads, delta-vs-fresh root-digest correctness invariant.
4. Repro v2 (Track 10): recorded journal with socket sequence numbers, CDD reducer (52% fewer queries than ddmin) with evidence priors + final ddmin 1-minimality pass, hermetic replayer (dead-proxy context, ordered fulfillment, seeded RNG/clock), standalone Python/Node/curl synthesizers with secret placeholders, portable vs strict signature strengths.
5. Hygiene follow-ups: initiator_top should skip __agent_internal_ hook frames when an attributed chain exists; exceptions should persist className/scriptId/exceptionId; only DomMirror may call DOM.getDocument (it invalidates nodeIds).
