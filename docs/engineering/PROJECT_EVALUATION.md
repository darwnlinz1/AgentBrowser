# AgentBrowser Project Evaluation — HYPER-BROWSER first batch

Date: 2026-10-15 (America/Los_Angeles) · Branch: `hyper/phase0-foundation`
Scope of this evaluation: the first implemented batch of
`docs/engineering/BREAKTHROUGH_ROADMAP.md` (Phases 0, 1, 2, 3, P + corpus +
benchmark harness), plus quality gates.

## What was built

| Phase | Deliverables (files) |
|---|---|
| 0 Foundation & safety | `pyproject.toml`, `requirements.txt`; cross-platform `find_system_browser` (`cdp.py`); `agent_browser/security.py` (loopback token, Host/Origin validation, path confinement) wired as daemon middleware; on-egress redaction (`evidence.redact_entry`) on traffic/cookies/storage/proxy/caller routes; `crypto-logs` consent gate; password/OTP/card strip in DOM snapshot; `Fetch.requestPaused` never dropped + drop counter; `on_drop` auto-reconnect |
| Corpus | `fixtures/fault_corpus.py` (10 archetypes with ground truth), `fixtures/fixture_server.py` (ephemeral port 0), `fixtures/metrics.py` |
| 1 Causal core | Stack-stamped actions (`evidence.make_action_tag`, async parent-chain walking, `attributed` confidence); deterministic quiescence (`quiescence.py`, 4 vectors, deadlock-proof cap); `type_text` focus fix (`DOM.focus`-equivalent + native setter + `Input.insertText`); CDP `Audits` CORS/CSP/cookie/mixed-content (`runtime.py`, `diagnostics.py`); failure-signature oracle + regression generator |
| 2 Agent-native | `session_registry.py` (session handles, ephemeral ports, multi-session routes); `axtree.py` + AXTree snapshot/view/resolve in `dom.py` (node-identity refs, Merkle hashes, changed-subtree diff); `mcp_server.py` (~18 tiered tools, redacted dispatcher, `eval`/`shutdown` never exposed) + `cli mcp --list-tools` |
| 3 Repro & verify | In-page `fetch` replay + `Network.replayXHR` + HAR export; response-stage match-and-replace (opt-in); `intervention.py` (k-of-k promotion); `minimizer.py` (flaky-aware ddmin); `verify.py` (OBSERVE..VERIFY gates + state machine) |
| P HYPER core | `codec.py` (msgspec hot path, stdlib fallback) wired into CDP send/receive; `transport.py` (NUL-framed pipe transport, POSIX fd 3/4 launch, clear Windows fallback); `ringbuffer.py` (critical events never shed, drop accounting); lightweight `light`/`hyper` Chromium launch profiles |

## Verification evidence (measured, this machine)

- Chrome-free suites: **69 passed** (`test_evidence_and_isolation`,
  `test_phase0_hardening`, `test_fault_corpus`, `test_phase1_causal`,
  `test_phase2_agentnative`, `test_phase3_repro`, `test_phaseP_hypercore`,
  `test_benchmark_harness`).
- Live Chrome (`python test_suite.py`): **7/7 passed** after the codec switch.
  Stack attribution is proven on a real page: the click's fetch initiator chain
  contains `__agent_internal_action_<id>-click.js`, confidence `attributed`,
  while the same fixture's background ping stays `temporal` in unit tests.
- Live AXTree: `[#1] button 'Trigger Broken Flow'` bound to `backendDOMNodeId`
  8 and resolved to a live rect — with zero attributes written into the page.
- Live in-page replay: `replay_kind=in_page_fetch`, status 200, identical-body
  diff; HAR export redacts cookies by default.
- Bugbot review of the diff: found 1 high (caller endpoint leaked raw request
  secrets) + 2 medium real defects — all fixed and re-tested. A fourth finding
  concerned a pre-existing root cookie script that this work did not add; it
  remains untracked and out of scope (see Risks).

## Benchmarks (measured here, `benchmarks/results/latest.{json,md}`)

| Vector | AgentBrowser | Competitor (measured only when installed) |
|---|---|---|
| Codec throughput | msgspec encode ~1.21M frames/s, decode ~639k/s | — (in-process) |
| Token density (synthetic 600-node page) | 1,231 action-graph tokens vs ~9,422 naive DOM tokens (**7.65x**) | Playwright aria snapshot is the design reference; small fixture page measured 10 tokens (toy page — not comparable at scale) |
| Ring buffer | ~2.75M events/s, dropped 0 | — |
| Cold start (headless Chrome) | ~1,536 ms (`light` profile, includes domain setup) | Playwright (system Chrome) ~175 ms launch-only — **AgentBrowser is slower to cold-start; this is the top footprint gap to close** |
| Browser tree RAM | ~437 MB (full Chrome process tree, light profile) | not measured for Playwright in this run |

Honesty note: Crawl4AI and Puppeteer were **not installed** in this
environment, so they are reported `not_installed` in the harness output —
no borrowed numbers.

## Architecture health

Strengths: the evidence model stays honest (temporal is never upgraded without
a signal; attributed requires the stamped chain; interventional requires
k-of-k A/B). Pure cores (evidence, quiescence, axtree, minimizer, verify,
registry, metrics) are Chrome-free testable, which is what makes the
corpus gate cheap. Daemon attack surface is materially smaller (token +
Host/Origin + confined writes + consent-gated sensitive surfaces).

Remaining gaps (ranked):
1. Cold start (~1.5s) vs Playwright launch (~0.18s): process-ready polling and
   per-domain setup dominate; pipe transport (POSIX) and lazier domain
   enablement are the levers.
2. Multi-session exists at the registry/route level, but per-frame session
   routing (OOPIF/worker command targeting) is not yet unified; the legacy
   injected-JS snapshot still covers same-origin frames only (AXTree is the
   path forward and is live-verified for the top frame).
3. MCP: the tool registry/dispatcher are complete and tested; the stdio
   transport needs the optional `mcp` package and a session-registry-backed
   provider for `session_open/close` (currently delegated to the transport
   layer and raising a clear error in the dispatcher).
4. Pipe transport is POSIX-only by platform design; Windows keeps the
   WebSocket transport (msgspec codec still accelerates both).
5. Response-stage interception is opt-in (`include_response=True`) so the
   default capture path is unchanged; `Fetch.continueResponse` availability
   varies by Chrome build, with a body-preserving fulfill fallback.
6. The fault corpus scores ground truth in Chrome-free form for all 10
   archetypes, but live-browser scoring is wired only through the existing
   integration suite (canonical case) — extending live scoring per archetype
   is a Loop task.

## Risks & scope

- Legitimate-use boundary held: no CAPTCHA/stealth/credential tooling was
  built; crypto/source surfaces stayed observe-only and consent-gated.
- **Git state warning:** this branch carried large pre-existing uncommitted
  work (including root-level live-secret capture scripts and JSON dumps)
  before this batch. Those files were not authored here, are fenced by
  `.gitignore` where they match secret patterns, and **must be reviewed
  file-by-file before any commit** — several root scripts (cookie extraction /
  session transfer helpers) should never be committed at all. For that reason
  this batch intentionally leaves its changes uncommitted for owner review
  rather than sweeping pre-existing work into a commit.
- Secrets: stored capture entries still hold raw headers in-process (needed
  for search); every egress route now redacts by default with an explicit
  `include_secrets` opt-in.

## Next cycles (Loop / Automation backlog)

1. Cold-start diet: lazy domain enablement + ready-wait on
   `DevToolsActivePort`, target < 500 ms.
2. Live per-archetype corpus scoring for all 10 cases (attribution P/R gate).
3. Frame/worker session routing on top of the registry; AXTree view becomes
   the default `view`.
4. MCP stdio provider backed by the session registry; publish tool catalog.
5. Self-healing fingerprints + record/replay flows (roadmap Phase 2/3 tails).

---

# Evaluation v2 -- completion audit (2026-10-15, after cycles 2-25)

Requirement-by-requirement audit against current tree state. Evidence is measured output, not intent.

| Requirement (roadmap/goal) | Evidence now | Verdict |
|---|---|---|
| Attribution precision/recall on corpus | fixtures/run_live_corpus.py live: 10/10 cases P=R=1.0, false-on-background=0, dropped_events=0 (also under AGENTBROWSER_STRICT_SETTLE=1) | Proven |
| Causal evidence ladder to the top rung | run_intervention_live.py live: control 2/2 reproduced, treatment 0/2, promoted=interventional | Proven |
| Minimization on real recordings | run_journal_minimize_live.py live: 82 journal events -> minimal {document, failing-endpoint}, 8 hermetic runs | Proven |
| Generated artifacts actually run | Node + Python repros exit 0 (bug) / 1 (fixed) against case 4, executed end-to-end | Proven |
| Daemon security contract | run_daemon_smoke.py subprocess: 401 unauth, 403 evil Origin, CLI via token, daemon exits | Proven |
| MCP as agent surface | 18 tools over real stdio, session registry backed, eval/shutdown absent | Proven |
| HYPER transport (no port) | pipe-windows live on Chrome 155 incl. full controller trace; Raw-envelope reply routing | Proven |
| Test gates | 114 Chrome-free passed; live suite 7/7; corpus 10/10 | Proven at last run |
| Code review gate | Bugbot pass 1 (4 findings, 3 real fixed) + stale-loaderId fix; follow-up pass over cycles 20-25 in flight at audit time | Mostly proven |
| Commit/PR per cycle | Intentionally NOT done: branch carries pre-existing uncommitted work incl. root live-secret scripts; owner must review file-by-file first | Blocked on owner (by design) |
| Scheduled Automation | Draft table presented; opening the editor needs owner approval per the Automations flow | Blocked on owner |

Stale v1 gaps now closed: MCP session provider (cycle 4/20 stdio live), live per-archetype corpus scoring (cycle 3), pipe transport on Windows (cycles 16-17; v1 wrongly assumed POSIX-only), OOPIF capture (cycle 8), cold-start attribution (segment profile: Chrome process launch itself ~1.5s on this Windows box; readiness polling and domain setup are not the bottleneck).

Genuinely remaining: (1) default view still the legacy injected-JS snapshot -- AXTree is available and budgeted but not yet the default; commands still target the active page session only (child sessions are captured, not yet individually driven); (2) per-session ordered lanes / Raw params for events (replies are Raw-routed; events still full-parse by design); (3) strict timer-ledger via main-world instrumentation (deliberately opt-in only, detectability tradeoff); (4) Crawl4AI/Puppeteer competitor numbers still not measured (not installed); Playwright comparisons on this machine are unreliable (driver lock + warm cache) and must be redone on a clean runner; (5) commit/PR + Automation arming await owner decisions above.
