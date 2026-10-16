# AgentBrowser autonomous progress

Date: 2026-10-09
Branch: `main` (pre-existing uncommitted work was left intact)

## Baseline

- Package is a CDP multiplexer plus DOM, network, runtime, tracer, diagnostics, storage, user tracking, focus, source inspection, a local mitmproxy engine, a FastAPI daemon, and a CLI.
- About 6k lines across `agent_browser/*.py`. No `requirements.txt` in the repo. Python 3.12 is present. `websockets` 17.1 exports `ConnectionClosed` at the package root; `websockets.exceptions` is not a reliable attribute.
- Existing tests (`test_suite.py`, `test_cli.py`, `test_edge_cases.py`, `test_user_tracking.py`, `test_crypto_reverse.py`, `test_proxy_interception.py`, `test_multiprofile_filter_sources.py`) are integration scripts. They launch Chrome or hit the network. They were not run in this iteration because they bind fixed debug ports and a leftover browser would be attached as the session. That is itself a defect, now refused in code.

## Architecture decision

Three architectures were compared against the code, not against a slide:

| Architecture | What it requires | Verdict |
|---|---|---|
| A. Keep index-window tracing and call it causal | Nothing. This is the previous code. | Rejected. `tracer.py` sliced `traffic_order` after `sleep` and wrote "Action triggered". Background requests in the settle window were reported as effects. |
| B. Evidence-labeled observation plus links only when a signal exists | A small pure module, no new protocol dependency. | Accepted as the first slice. Temporal co-occurrence stays visible and is labeled `confidence=temporal`. A causal link is emitted only for URL overlap (`supported`) or a single failing request plus a null-consumer exception (`weak`). |
| C. Full action-to-effect graph with initiator-stack proof, load-event waits, and differential replay | CDP initiator parent walking, execution-context attribution, `Page.loadEventFired` filtered by `loaderId`, a new storage format. | Not implemented. The current tracer never reads `initiator`. Building the graph before the honesty fix would just store false edges in a richer schema. |

CDP event collection is not sufficient for reliable causal tracing. Chrome gives initiator stacks and monotonic timestamps; this process gives wall-clock sleeps and list indexes. Those are different clocks. A graph that hides that difference would be worse than the current report.

Deterministic cross-run replay is not available. `replay_request` is a fresh `requests` call, not a browser replay. It does not share the cookie jar, HTTP/2 session, or redirect chain.

## Confirmed defects fixed in this iteration

1. Trace reports claimed causation from an index window. `agent_browser/evidence.py` now builds the report. `requests_triggered_count` is kept as a compatibility alias of `requests_observed_count`.
2. Diagnostics paired every "undefined" exception with every failing request in the last 30 and said "likely caused". `fr_url` was computed and unused. Replaced with `correlate_diagnostics`. Unrelated pairs return `insufficient_evidence`.
3. `CDPConnection.disconnect()` and websocket close left pending `send()` futures to hit the full timeout. Pending futures are now failed immediately. Listener close matches both `websockets.ConnectionClosed` and `websockets.exceptions.ConnectionClosed`.
4. `resolve_profile_dir("../../../evil_escape")` and `resolve_profile_dir("../escape")` resolved outside the profiles root because any name containing a separator was treated as a directory. Relative names are now a single sanitized segment under `profiles/`. Absolute `profile` and explicit `profile_dir` remain caller-chosen paths.
5. Containment used `startswith(profiles_root)`, which also matches a sibling `profiles_evil`. Containment now uses `os.path.commonpath`.
6. `launch()` attached to whatever was already on the debug port, including another profile. It now refuses unless `/json/version` `userDataDir` matches. A launch timeout kills the process it started.
7. Cookie URL for `127.0.0.1:8080` was `http://127.0.0.1:8080:/` because the path was appended after a colon.
8. Redirects overwrote `requests[id]` and appended the same id again. The prior hop is kept on `redirect_hops` and the id is not duplicated.
9. Generated reproduction scripts interpolated URL and selector into Python source. They are now `json.dumps` literals.
10. `--remote-allow-origins=*` was replaced with the loopback debugger origin. Replay no longer sets `verify=False`. The local proxy no longer sets `ssl_insecure=True`. AI traffic summary prints `Cookie: [redacted]` instead of the header value. Password input values captured by the user tracker are stored as `[redacted]`.
11. Mutation observer could exceed its cap inside one `childList` record and never reported truncation. It now stops at 200 and returns `truncated`.

## Tests executed

2026-10-09, Chrome at `C:\Program Files\Google\Chrome\Application\chrome.exe`.

| Command | Result |
|---|---|
| `pytest test_evidence_and_isolation.py -q` | 16 passed |
| `python test_suite.py` | 7/7 passed. Port 9225. Trace labeled the API request `temporal` and named the initiator `window.fetch`. |
| `python test_edge_cases.py` | 4/4 passed. Port 9227. |
| `python test_user_tracking.py` | passed. Port 9240. |
| `python test_crypto_reverse.py` | passed. Port 9241. |
| `python test_proxy_interception.py` | passed. HTTP through the proxy still records the flow after `ssl_insecure` was removed. |
| `python test_multiprofile_filter_sources.py` | passed. Ports 9228-9230. Traversal names stayed inside `profiles/`. |
| `python test_cli.py` | passed, but it talked to a **different checkout's daemon** already bound to 127.0.0.1:9224 (`OpenBullet-Release\AgentBrowser`). That process was not modified. |
| This tree, daemon `--port 9334`, browser port 9234 | `start --headless --profile evidence_live` created `Downloads\AgentBrowser\profiles\evidence_live`. `open http://127.0.0.1:1` returned `navigated: false` and `net::ERR_UNSAFE_PORT`. `open http://example.com` then `eval document.title` returned `Example Domain`. |

Latent bugs found by that live run and fixed:

- `agent_browser --server URL start` ignored `--server` and `--profile` because the `start` subparser redefined them with defaults. A command aimed at this tree started a browser in the other checkout.
- `POST /navigate` always returned `navigated: true`. Chrome can return `errorText: net::ERR_CONNECTION_REFUSED` inside a successful CDP result. The route now returns `navigated: false` and `error`.
- `start --headless` was dropped when the parent parser also defined `--headless`. Subparser values now fall back to parent values only when the subparser left them unset.

## Follow-up fixes after the protocol comparison

- `navigate` waits for `Page.loadEventFired` on the same session, up to 8 seconds, and records `{loaded, loaderId}` on the result. It no longer returns after a fixed 1 second sleep.
- A newly attached page session gets Page, DOM, Network, Runtime, the user tracker, and the source inspector enabled. It becomes the active tab only when no page is active. Detach now also clears `active_target_id`.
- CDP handler tasks are capped at 64. Past that, the coroutine is closed instead of queued without bound.
- `Log.entryAdded` is stored. `Log.enable` was previously sent and ignored, so CORS and security messages never reached diagnostics.
- `Runtime.evaluate` failures are appended to the exception list. They used to be returned to the caller and then disappear from `diagnose`.
- A redirect hop is frozen as its own entry (`{requestId}#redirect-N`) with status, headers, protocol, remote address, and an explicit `redirect_body_unavailable` gap. The follow-up request keeps the original id. CDP does not provide a body for the redirect response; that is recorded, not invented.
- WebSocket created/closed and EventSource messages are stored. Frames alone had no URL and no close.
- A favicon or image 404 no longer cancels the single-failure signal. Live `test_suite.py` showed diagnose saying "not attributed" because `favicon.ico` 404 sat next to the one API 500. After the filter, diagnose reports `single_failure_null_consumer` against `/api/failing-endpoint`.
- Focus compare uses 8000 characters and records the real `outerHTMLLength`, so a change past the sample is visible as a length change.

## Still not claimed

- Shadow DOM is not walked. Same-origin iframe refs are still snapshotted in the top frame.
- `headersText` is absent on HTTP/2 and HTTP/3. Multipart file parts are omitted by `getRequestPostData`. Redirect bodies are unavailable. These are protocol gaps.
- Stored request entries still contain Cookie and Authorization so traffic search can match them. The AI summary redacts the cookie line.
- No websocket reconnect. No differential replay.
- Network and diagnostic stores still retain `Cookie` and `Authorization` on the request entry itself. Only the AI summary and the network-failure issue details were narrowed. A full redaction pass would break tests and callers that search traffic by cookie.
- Handler tasks are logged but not bounded. A slow handler can still pile up.
- No reconnect after websocket loss.
- No differential replay, no reproduction minimizer, no adaptive instrumentation. Those were researched and deferred until the evidence labels are stable under a live browser.

## Next tasks

1. Redact `authorization` / `cookie` on stored entries behind an explicit `include_secrets` flag.
2. Reconnect the CDP websocket after a drop, with a generation counter so old futures stay failed.
3. Walk open shadow roots in the snapshot, and route iframe refs to the child session.

## 2026-10-15 HYPER-BROWSER first batch (branch hyper/phase0-foundation)

- Phases 0/1/2/3/P implemented per docs/engineering/BREAKTHROUGH_ROADMAP.md: packaging + daemon hardening + on-egress redaction; seeded 10-archetype fault corpus (fixtures/); stack-stamped attributed causal links (proven live: initiator chain contains __agent_internal_action_<id>); quiescence settle; Audits CORS/CSP; session registry + ephemeral ports; AXTree node-identity refs with Merkle diff (live-verified); MCP tool registry/dispatcher; in-page replay + HAR; response-stage match-and-replace; interventional A/B; ddmin minimizer; VERIFY gates; msgspec codec + pipe transport + ring buffer + light/hyper launch profiles.
- Tests: 69 Chrome-free passed; live test_suite.py 7/7. Benchmarks in benchmarks/results/latest.md (codec ~639k decode/s, AXTree 7.65x, ring 2.7M/s; cold start ~1.5s vs Playwright ~0.18s launch -- top gap).
- Bugbot review: caller-endpoint secret leak + 2 medium defects fixed and re-tested.
- NOT committed: branch carries pre-existing uncommitted work incl. root live-secret scripts; owner must review file-by-file before any commit. Full detail: docs/engineering/PROJECT_EVALUATION.md.

## 2026-10-15 round-2 deep-dive fold-in

All 4 HYPER deep dives returned (zero-copy transport, Merkle AXTree, quiescence, minimal-repro). Quick wins applied + tested (71 Chrome-free passed, live 7/7): websockets compression=None; profile ownership via DevToolsActivePort when /json/version lacks userDataDir; Debugger.setSkipAllPauses (harvest never freezes on debugger;); Page.setLifecycleEventsEnabled; stale data-agent-ref cleanup; initiator_top skips agent hook frames; replay write-gate (allow_write, CLI --allow-write) + replay route off the event loop; .gitignore widened (*.har, journals/, repro/, *storage*.json). Larger designs (transport v2 lanes/Raw envelopes, quiescence monitor v2, keyframe+delta policy, CDD minimizer + hermetic replayer) recorded as prioritized backlog in BREAKTHROUGH_ROADMAP.md.

## 2026-10-15 cycle 2: cold-start diet + navigation v2

- Readiness now polls <profile>/DevToolsActivePort at 25ms (was /json/version every 250ms); page-session domain enables run concurrently; wait_for_navigation records loaderId-filtered lifecycle loads and resolves same-document (hash/history) navigations via navigatedWithinDocument instead of burning the full timeout. 74 Chrome-free passed, live suite still 7/7 (prior check).
- Segment profiling: launch (process->ws ready) 1519ms of the 1521ms controller.start total; ws connect 9ms. Cold start on this Windows box is Chrome process launch itself, not readiness polling or domain setup. Playwright comparison on this machine is unreliable (driver CreateProcess WinError 32 lock + warm-cache ordering) -- do not quote the earlier 175ms as a like-for-like loss; re-measure on a clean runner.

## 2026-10-15 cycle 3: live corpus scoring

fixtures/run_live_corpus.py scores all 10 archetypes on real Chrome. First run 8/10: race ground truth missed /api/slow (both fetches are action-caused; corpus fixed) and debounced-input recall 0 (trusted Input.insertText carries no action tag; type_text now inserts via the stamped script with execCommand, Input.insertText as fallback). Also fixed a false-early settle: a fresh QuiescenceTracker read as quiet since the epoch. Result: 10/10 PASS, precision=recall=1.0, false-on-background=0, dropped_events=0. 75 Chrome-free passed, live suite 7/7.

## 2026-10-15 cycle 4: MCP sessions

LocalSessionManager backs the MCP server with SessionRegistry (isolated profile + ephemeral port per session); dispatcher delegates session_open/close/status. Adapted build_server to MCP SDK v2 (MCPServer, explicit per-tool signatures). Live stdio smoke: 18 tools listed, session_status returns registry state, eval/shutdown not exposed.

## 2026-10-15 cycle 5: self-healing fingerprints

agent_browser/fingerprints.py (capture/similarity/resolve with fail-closed threshold + heal evidence), wired into DOMEngine (fingerprints stored on successful query, resolve_healed_ref over a fresh snapshot, heal_log). 80 Chrome-free passed, live suite 7/7.

## 2026-10-15 cycle 6: budgeted AX keyframe

render_budgeted_view enforces a hard token budget (default 400) with a fold line for the remainder; format_axtree_view uses it. Policy per round-2 research: explicit view always returns a full keyframe; Merkle diffs stay supplementary, never the sole model state. 81 Chrome-free passed.

## 2026-10-15 cycles 7-8 (loop tick 1)

Cycle 7: CDD minimizer (prior-ordered, final ddmin 1-minimality pass; correctness pinned, query-race claim corrected for single-essential cases) + normalized signatures (evidence.url_template, V8 message templates) wired into failure_signature. Cycle 8: engines take explicit session_id; attached iframe/worker/service_worker targets get Network/Runtime/Debugger enabled without stealing the active page; recursive Target.setAutoAttach on page sessions. Live OOPIF check: cross-site iframe got its own session and its /child-api fetch was captured. Chrome-free 87 passed.

## 2026-10-15 cycles 9-10

Cycle 9: synchronous CDP subscribers (inline, never shed by the async task cap; errors counted) + NetworkActivityMonitor ledger wired into tracer settle with traffic-buffer fallback; settle reports activity_source. Cycle 10: agent_browser/synth.py builds curl reproductions from captured requests -- secrets become AB_* env placeholders, writes refused without allow_write. Chrome-free 92 passed at this point.

## 2026-10-15 cycles 11-12

Cycle 11: flows.py record/replay ( Flow/FlowStep JSON, ${VAR} substitution, fingerprint heal retry with evidence). Cycle 12: replay.py ExchangeIndex (sequence-aware, keep-set for minimization) + NetworkEngine hermetic mode -- Fetch pauses fulfilled from recording or stubbed, structurally no continueRequest/live path. Live-verified: recorded fixture page re-renders from the recording under hermetic navigation.

## 2026-10-15 cycles 13-14

Cycle 13: response bodies fetched by a bounded pool (semaphore 6, cap 256, shed counted as capture_gap) instead of inline in the loadingFinished handler; Network.configureDurableMessages enabled (guarded) so pooled fetches survive cross-process navigation. Cycle 14: stamped-probe settle verification -- after quiet, one Runtime.evaluate probe must observe an unchanged activity_seq or the settle re-waits (stale quiet readings are discarded). Chrome-free 101 passed.

## 2026-10-15 cycles 15-16

Cycle 15: opt-in flight-recorder journal (AGENTBROWSER_JOURNAL) -- sync subscriber, seq-stamped JSONL, secrets redacted at write, byte-capped with truncation marker. Cycle 16: Windows --remote-debugging-io-pipes transport ported into agent_browser/transport.py (PipeCDPClient thread-IO + spawn_chrome_pipe_windows); live-verified against Chrome 155: getVersion/getTargets over the pipe, Chrome exits rc 0 on pipe close, no TCP port opened. Controller integration (transport selection) is the next step. Chrome-free 104 passed.

## 2026-10-15 cycle 17: pipe transport integrated

AgentBrowserController accepts transport='pipe-windows' (or AGENTBROWSER_TRANSPORT), spawning Chrome via spawn_chrome_pipe_windows and driving the full engine over the pipe client; server StartSessionRequest gained the transport field. Live: navigate + click trace on the 500 fixture over pipe -- attributed 500, 1 exception, settle quiescent with stamped probe, clean stop, no TCP port. WebSocket remains the default/attach transport.

## 2026-10-15 cycle 18: journal-driven hermetic minimization

Journal loader + build_exchanges (journal-only exchanges stub-only; bodies merge from network/HAR) + evidence-prior exchange units. Live minimization on corpus case 4 first produced a false minimal {failing-endpoint} because the Document stub was 204 (a navigation no-op -- the replay cheated on a stale page); fixed the stub to a blank 200 document, kept resource_type on exchanges, and demoted favicon priors. Re-run: minimal = {document, failing-endpoint} in 5 predicate runs -- exactly the ground-truth essential set.

## 2026-10-15 cycles 19-20

Cycle 19: standalone synthesizers in synth.py -- Python (agent_api) and zero-dependency Node (CDP over --remote-debugging-pipe), both embedding one double-encoded JSON spec so recorded data can never become code; Node form live-verified (bug->exit 0, fixed->exit 1; a finally-block cleanup bug that masked exit codes with 125 was found and fixed). Cycle 20: fixtures/run_intervention_live.py promotes the case-4 hypothesis live -- control 2/2 reproduced, treatment (fulfilled good response) 0/2, confidence=interventional.

Corrective note (cycle 19): a StrReplace in synth.py silently absorbed build_curl's signature into build_node_repro (dead code after return, still parseable). Cycle-local tests passed; the full-suite gate caught the ImportError. build_curl restored; 108 Chrome-free passed. Rule reinforced: every cycle finishes with the FULL suite, never only its own test file.

## 2026-10-15 bugbot fix: stale lifecycle load

Bugbot (high): wait_for_navigation resolved on any Page.lifecycleEvent load. Fixed: lifecycle loads are buffered pre-reply and only resolve when loaderId matches this navigation (re-scanned after the reply arms the filter); lifecycleLoaderId no longer falls back to an arbitrary buffered load. Regression test emits a stale-loader load first and asserts the wait stays pending until the matching load arrives.

## 2026-10-15 cycle 20: synthesizer e2e

Interventional confirmation verified live first (control 2/2 reproduced, treatment 0/2, promoted=interventional). Then generated artifacts were executed, not just compiled: Node repro exit codes correct in bug/fixed modes on first run; Python repro failed with ModuleNotFoundError when placed outside the repo -- template now bootstraps the package path (AGENTBROWSER_HOME / script dir / cwd). Both artifacts now exit 0 for --expect bug and 1 for --expect fixed against corpus case 4.

## 2026-10-15 cycle 21: journal-driven minimization

agent_browser/journal_minimize.py merges journal exchanges (order/priors) with network-buffer bodies, then CDD (async) removes exchanges while hermetic replay reproduces the signature. Live on case 4: 82 journal events -> 3 exchanges -> minimal {document, /api/failing-endpoint} in 8 hermetic predicate runs; background ping and favicon minimized away. MINIMIZATION PASS.

## 2026-10-15 cycle 23: daemon smoke

fixtures/run_daemon_smoke.py drives the real daemon + CLI as subprocesses on ephemeral ports: status open path OK, unauthenticated start 401, cross-site Origin 403, CLI start/open/view/click succeed via the token file with the canonical attributed trace (500 attributed, ping temporal, 1 exception), daemon exits cleanly on stop. Cycle 22 (type settle floor) was found already implemented with tests by parallel loop work and verified rather than duplicated.

## 2026-10-15 cycle 23 addendum: CLI profile bug

Daemon smoke exposed that --profile before the start subcommand was overwritten by the subparser default (daemon fell back to debug_browser_profile). Fixed with argparse.SUPPRESS defaults on the start subparser; smoke now asserts profile_name == requested (10/10 PASS).

## 2026-10-15 cycles 24-25

Cycle 24: Raw-envelope CDP reply routing -- listener decodes only the msgspec envelope (id/method/sessionId, params/result Raw) to route replies; send() parses the result slice on demand. Events still full-parse. Cycle 25: opt-in strict settle probe (AGENTBROWSER_STRICT_SETTLE) requiring a deep requestIdleCallback (idle, timeRemaining>=8ms, no aria-busy) on top of the stamped activity probe. Chrome-free 114 passed; strict-mode live corpus PASS. Note: a leftover test Chrome (debug_browser_profile, port 9223) from generated-repro runs can lock the profile and make test_suite fail at launch with 'Browser process exited before DevTools was ready' -- kill that process tree and rerun (suite 7/7 after cleanup).

## 2026-10-15 bugbot follow-up fixes (cycles 20-25 review)

Three real findings fixed with regression tests: (1) wait_for_navigation no longer resolves on a pre-reply Page.loadEventFired (stale previous-document load); (2) additional page tabs now get the full session enable (Page/DOM/lifecycle/network/runtime/tracker/hooks with explicit session ids) instead of the child capture-only enable, so a promoted tab is fully usable -- user_tracker/source_inspector enable() learned explicit session routing; (3) format_ai_summary redacts secret fields in request AND response body previews (JSON-recursive + form/Bearer patterns), not just headers.

## 2026-10-15 cycles 27-28

Cycle 27: accessibility-tree view is now the default get_agent_view (budgeted keyframe, AGENTBROWSER_VIEW=legacy opt-out, legacy fallback); numeric #ref clicks route to the snapshot that minted them -- AX refs click by node identity via DOM.resolveNode + callFunctionOn (live-verified; AX clicks are temporal for attribution since callFunctionOn cannot be sourceURL-stamped, CSS-selector clicks keep the stamped path). Cycle 28: controller list_tabs/activate_tab (+ /targets, /targets/activate, CLI tabs/activate-tab); every attached page tab is drivable; tab URLs refreshed from Page.frameNavigated. Live two-tab check: activated the second tab and viewed its page.

## 2026-10-15 cycles 27-30 (addendum)

Cycle 29: MCP tabs_list/tab_activate tools (20 tools total). Cycle 30: benchmark harness now measures envelope routing honestly -- at event-frame sizes envelope decode is comparable to full decode (741-919k/s vs 792-824k/s), but routing replies with a 20KB result is 616k/s vs 148k/s full decode (4.2x), which is the actual cycle-24 win (the listener no longer parses large results just to route them).

## 2026-10-15 cycle 31: docs coherence

SKILL.md gained tabs/activate-tab/mcp rows plus platform notes (AX default view, evidence grades, daemon token/redaction, pipe transport, verification harnesses); README gained install/test-harness/platform-notes sections pointing at the roadmap and evaluation. test_docs_coherence.py now fails if SKILL documents a non-existent CLI command or drops a key one.

## 2026-10-15 cycle 32: source reconstruction

agent_browser/source_reconstruct.py rebuilds the original tree from source-map sources/sourcesContent (webpack paths normalized, traversal-safe), decodes inline data: maps, inventories fetch/axios/XHR endpoints as URL templates, and writes trees only through confined paths. Wired as SourceInspector.reconstruct_sources + GET /source/reconstruct + CLI 'reconstruct'. Live: inline data: map reconstructed src/api/users.ts and inventoried /api/users/{var}.

## 2026-10-15 cycle 33: full gate sweep

One-pass battery, all green: 127 Chrome-free pytest; live suite 7/7; live corpus PASS (dropped=0); daemon smoke PASS; interventional promoted=true; journal minimization PASS; MCP stdio (20 tools) and pipe-transport controller trace verified in the same session.

## 2026-10-15 cycle 34: pre-commit review package

fixtures/build_commit_manifest.py classifies all dirty paths (commit / review-diff / never-commit), secret-scans candidates, and verifies gitignore coverage -> docs/engineering/COMMIT_REVIEW.md. Current: 66 safe-to-stage, 20 review (owner WIP interleaved in tracked files), 23 never-commit (root experiment scripts + dumps; cookie/state dumps verified ignored, cart_data_raw.json now ignored too). Secret scan: 0 hits outside test fixtures after replacing a docs 'forged_token' example with the AB_AUTHORIZATION placeholder. Nothing staged.

## 2026-10-15 cycle 35: web-vitals perf engine

agent_browser/perf.py: in-page PerformanceObserver vitals (LCP element, CLS excluding hadRecentInput shifts, long tasks, navigation timing) graded on standard thresholds (missing data = unknown, never good), plus CDP Performance renderer counters. Wired to controller, GET /perf/vitals, CLI 'perf', MCP perf_vitals (21 tools). Live on the race fixture: LCP 24ms (button), CLS 0, JSHeap ~1MB.

## 2026-10-15 cycle 36: dialogs + downloads

JavaScript dialogs are always answered (default dismiss, AGENTBROWSER_DIALOG_POLICY=accept to opt in) and logged; downloads are confined to agent_output/downloads with lifecycle tracking. Routes /dialogs, /dialog/policy, /downloads + CLI dialogs/dialog-policy/downloads. Live: alert auto-dismissed without hanging evaluate, confirm returns false under dismiss.

## 2026-10-15 cycle 38: repro isolation

Generated Python reproductions (synth.build_python_repro, diagnostics legacy + regression templates) now launch on find_free_port() with an isolated 'ab-repro' profile instead of the default 9223/debug_browser_profile -- the exact collision behind the leftover-Chrome profile lock observed in cycle 25. E2E re-verified (bug exit 0, fixed exit 1).

## 2026-10-15 commit (owner-approved scope)

Committed 116f347 'feat: HYPER-BROWSER engine' (100 files, safe group + tracked engine files + SKILL/README/test_suite for coherence; standalone staged tree verified 132 passed before committing). Never-commit group excluded per COMMIT_REVIEW.md. Push blocked: no GitHub SSH key on this machine and HTTPS credential flow needs interactive sign-in -- owner to run: git push -u origin hyper/phase0-foundation, then open the compare URL in the PR step.

## 2026-10-16 cycle 39: stale AX ref revalidation

AX refs are revalidated against Accessibility.getPartialAXTree before resolve/click: role/name identity changes mark the ref stale; a unique fresh ref with the same identity becomes the reanchor target (including remounted/gone nodes), while zero or multiple matches fail closed instead of clicking a guess. Live on Chrome: a replaced button was reanchored from backendNodeId 8 to 13 and the click still fired its fetch.

## 2026-10-16 cycle 40: human handoff

Controller handoff protocol: start_handoff pauses the agent (navigate/trace/evaluate/screenshot refused), shows an in-page 'human in control' banner, and complete_handoff resumes with a summary of the human actions captured meanwhile. Routes /handoff, /handoff/start, /handoff/complete + CLI handoff start|status|complete. Live: banner present, trace blocked during handoff, resume works.

## 2026-10-16 cycle 41: WebSocket/SSE semantics

agent_browser/stream_decode.py classifies WebSocket payloads (Socket.IO events, JSON, heartbeats, binary) and SSE messages; NetworkEngine now records socket lifecycle (created/closed, per-direction frame counts) and Network.eventSourceMessageReceived, and the AI traffic summary gains a decoded STREAMS section. Live: bidirectional Socket.IO chat decoded by event name, SSE reading parsed to {temp: 21}.

## 2026-10-16 cycle 42: WASM inspection

agent_browser/wasm_inspect.py parses WASM section tables, function counts, imports and exports from raw bytecode; SourceInspector captures scriptLanguage/is_wasm, fetches bytecode via Debugger.getScriptSource, and streams disassembly via Debugger.disassembleWasmModule with a chunk cap (an uncapped loop bug was caught by tests). Live: an in-page compiled module appears as wasm://..., exports 'f', and disassembles to real WAT.

## 2026-10-16 cycle 43: protobuf/gRPC-Web decoding

agent_browser/protobuf_decode.py decodes the protobuf wire format without a schema (varints, fixed, length-delimited, heuristic nested messages) and splits gRPC-Web flag+length frames including trailers. Binary WebSocket frames now attempt a blackbox protobuf decode before falling back to an opaque label. Wire-level facts only -- field meanings require a schema and are not guessed.

## 2026-10-16 cycle 44: metadata + DOM fallback hygiene

Runtime exceptions now persist className/scriptId/exceptionId and an origin (uncaught vs promise rejection), for both page exceptions and Runtime.evaluate failures. The page-HTML DOM fallback in dom.py and source_inspector.py addresses the root by backendNodeId at depth 0 instead of a depth:-1 getDocument that invalidates frontend nodeIds.

## 2026-10-16 cycle 45: DOMSnapshot layout join

AX snapshots can join DOMSnapshot layout onto refs (document rects, paint order, viewport membership, and occlusion when a higher-paint-order box covers the ref center); the default AX view requests it and marks refs [occluded]/[offscreen]. Merkle hashing deliberately excludes geometry, so scrolling does not churn diffs.

## 2026-10-16 cycle 46: CLI --page routing + popup release fix

CLI gained a global --page <session_id> that activates a tab before any command. Live debugging exposed a root bug: auto-attached popup targets never navigated (and the opener's Runtime.evaluate hung) because Chrome holds them until Runtime.runIfWaitingForDebugger -- now sent after domain setup for page and child sessions, Puppeteer TargetManager order. Verified direct (window.open returns, popup navigates) and via daemon CLI (--page view renders the popup's page).

## 2026-10-16 cycle 47: scoped AX pin re-reads

DOMEngine.get_scoped_axtree resolves a selector (or #ref) to a backend node and re-reads only that subtree via Accessibility.queryAXTree, returning refs plus a Merkle diff against the previous scoped read. FocusEngine exposes it per pin and /focus/view includes ax_regions. Live: a body pin re-reads the Hydrate button scoped, without a full-page capture.

## 2026-10-16 cycle 48: DebugLoop orchestrator (loop tick)

agent_browser/loop_engine.py drives one OBSERVE->INSPECT->TRACE->DEBUG->MODIFY->REPLAY->VERIFY episode: failure signature from the trace, causal edge = failing request tied to the action (a successful attributed request after a fix is not the edge), VERIFY via the positive-artifact gates in verify.py. Live on case 4 with an interception fix: state DONE, all 6 gates pass, symptom absent across 3 reruns, time-to-root-cause 295.8ms (fixtures/run_debug_loop_live.py).

## 2026-10-16 cycle 49: debug loop surfaces

DebugLoop is now reachable from every operator surface, not just the Python API: POST /debug/episode (url, action, target, verify_runs), CLI `debug-loop <url> --target <#ref>`, and MCP tool `debug_episode` (diagnostics tier, 22 tools total). No fix hook travels across these boundaries, so VERIFY honestly reports BLOCKED while the bug persists; the fixed path stays Python-API/MCP composition. Gate: 27 Chrome-free passed across loop/perf/MCP/docs/hardening suites.

## 2026-10-16 cycle 50: consolidated gate sweep + DebugLoop input hardening

Full post-cycle-49 sweep, all green with no regression: 159 Chrome-free pytest passed (up from the 132 baseline at first commit; one new test added this cycle); live fault corpus 10/10 PASS with attribution precision = recall = 1.0, false-background = 0, dropped events = 0; live integration suite 7/7; live DebugLoop episode DONE/verified with 6/6 gates and time-to-root-cause 284.7ms. Code review of the cycle-49 surface diff found two hardening gaps, fixed at the shared core so server, CLI, and MCP all inherit them: DebugLoop now rejects unknown actions with ValueError before any page work (previously a silent no-op episode via tracer fall-through), and verify_runs is clamped to 1..10 (previously unbounded reruns could turn one API call into a long trace loop). The clamped value is reported back as verify_runs.

## 2026-10-16 cycle 51: lazy event routing + envelope resurrection (loop tick)

CDP handlers can now declare method interest (add_event_handler/add_sync_handler methods=...); the listener routes events by the cheap envelope and skips the full params parse when nobody declared interest, counted as skipped_events (surfaced in /session/status). NetworkActivityMonitor declares its 4 ledger methods; the journal and the controller dispatcher stay catch-all. PipeCDPClient has parity. Building this exposed a real latent bug: the Envelope Struct typed its Raw fields as Raw | None, which msgspec resolves to the None branch -- decode_envelope returned None for every non-empty frame, so cycle-24 Raw-envelope reply routing was silently dead and replies only worked via the full-parse fallback (and the old benchmark timed the instant-None path). Fields are now Raw = Raw() with decode_raw treating empty as absent. Honest re-measurement: envelope routing 1.46M frames/s vs full decode 886k/s on event frames; ~6.2x on a nested 200KB CDP-shaped reply; ~0.93x (scan-bound worst case, now reported separately in the harness) on a flat 20KB string body. Gates after the fix: 165 Chrome-free passed; live corpus 10/10 P=R=1.0 dropped=0; live suite 7/7; daemon smoke PASS; DebugLoop live DONE/verified.

## 2026-10-16 cycle 52: per-session FIFO event lanes (loop tick)

Roadmap Track 1 item landed: async CDP handlers no longer run as unordered capped tasks. CDPConnection now keeps one FIFO lane (asyncio.Queue, maxsize 1024) per sessionId; a worker per lane awaits handlers sequentially in arrival order, so per-session event ordering is guaranteed (loadingFinished cannot overtake requestWillBeSent) while one stalled session cannot block another. Overflow spills to the previous capped task pool with lane_spilled counted and surfaced in /session/status; Fetch.requestPaused still takes the never-drop path. PipeCDPClient has parity with loop-side lanes fed via call_soon_threadsafe. This supersedes the cycle-9 async cap-shed semantics: that test now pins the surviving invariant (sync subscribers run inline, ahead of async lane work). Gates: 168 Chrome-free passed (3 new lane tests: ordering, cross-session isolation, counted spill + critical Fetch); live corpus 10/10 dropped=0; live suite 7/7; daemon smoke PASS.

## 2026-10-16 cycle 53: narrow Fetch patterns (loop tick)

Fetch.enable no longer pauses every request when rules exist. enable_interception derives Request-stage patterns from the rule set: derive_cdp_url_pattern accepts only a restricted regex shape (literals, escaped literals, .*, anchors) whose longest literal run (>=3 chars) must appear verbatim in every matching URL, so *<run>* provably over-matches and never under-matches; alternation, classes, quantifiers, or class escapes widen the whole set back to '*'. Python-side regex still decides each paused request, Response stage stays wide by design, and hermetic replay keeps '*' because it must pause everything to fulfill from the recording. Gates: 183 Chrome-free passed; live interventional still promoted=true (control 2/2, treatment 0/2) and DebugLoop DONE/verified with the narrowed */api/failing-endpoint* pattern.

## 2026-10-16 cycle 54: evaluation v3 (loop tick)

PROJECT_EVALUATION.md gained a v3 audit section: every v2 'genuinely remaining' item mapped to its closing cycle (AX default view 27, tab driving 28/29/46, lazy event routing 51, FIFO lanes 52, commit/push after owner manifest review), the new capability list (cycles 40-53), and fresh measured numbers (183 Chrome-free; corpus 10/10 P=R=1.0 dropped=0; envelope route ~1.48M frames/s after the cycle-51 resurrection, with the flat-string 0.93x worst case stated). It also records, against our own earlier claim, that the cycle-24 envelope optimization was inactive until cycle 51 -- the audit corrects the 'Proven' row rather than letting it stand. Remaining honestly-open items: opt-in-only strict timer ledger (by design), competitor numbers needing a clean runner, the unadopted CORS loader flag (no measured effect), and PR creation (owner action).

## 2026-10-16 cycle 55: AX delta-vs-fresh digest invariant (loop tick)

Track 4's correctness invariant landed: axtree.refs_digest (canonical (backend_id, fingerprint) digest; ref numbering excluded as presentation), apply_ref_diff (reconstruct new state from old + delta), and delta_consistent. Both get_axtree_snapshot and get_scoped_axtree now check the invariant on every capture and report delta_consistent, so a lying delta is detectable at runtime and callers can fall back to the full keyframe. Property tests: each mutation kind (rename, state toggle, add, remove, no-op) reconstructs exactly, plus 60 seeded random mutation sequences. Gates: 187 Chrome-free passed; live suite 7/7.

## 2026-10-16 cycle 56: portable vs strict signature strengths (loop tick)

Track 10's last item: failure signatures now carry path_only (evidence.url_path_template: the url_template with host:port stripped) alongside the strict path, and loop_engine.symptom_matches takes a strength. strict (default, unchanged) requires exact templated host+path and exact status; portable accepts any failing status on the same path template across ports/hosts, but still requires the exception class, so background noise cannot satisfy it. DebugLoop.run_episode accepts signature_strength, validates it, and reports it -- a portable VERIFY pass is never silently presented as a strict one. Gates: 192 Chrome-free passed; live DebugLoop DONE/verified under strict.

## 2026-10-16 cycle 57: SPA route windows (loop tick)

Track 8's route-window item: NetworkActivityMonitor now tags every in-flight request with a route epoch, advanced by Page.navigatedWithinDocument and main-frame Page.frameNavigated (iframe navigations do not open a window), and reports in_flight_current_route / in_flight_previous_routes. QuiescenceTracker.note_route_change opens a fresh epoch: previous-route stragglers move to stale_in_flight (reported, no longer blocking), while the route change itself counts as activity so the new window still needs a full quiet window. The tracer poll syncs epochs from the monitor snapshot, so an action that triggers an SPA navigation settles against the new route only. Gates: 195 Chrome-free passed; live corpus 10/10 dropped=0 settle=quiescent.

## 2026-10-16 cycle 58: lane lifecycle on socket drop (loop tick)

Review of the cycle-52 lanes found a lifecycle hole: lanes were only closed in disconnect(), so on an unexpected socket drop the queued handler calls and lane workers survived -- stale events could execute against a later reconnected session. CDPConnection now closes lanes in the ConnectionClosed path too, PipeCDPClient closes them on disconnect and on reader-thread exit, and queued coroutines discarded at close are counted in dropped_events (never silent). Tests pin it: with a parked first handler, r2/r3 queued behind it never run after the drop and dropped_events == 2. Gates: 197 Chrome-free passed.
