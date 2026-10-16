"""
Opt-in, observe-only timer ledger for strict settling.

Main-world timer instrumentation is a detectability tradeoff, so this is
OFF by default and only installed when the operator explicitly enables it
(``AGENTBROWSER_TIMER_LEDGER=1``) for their own debugging/QA targets.

The installed script only *observes*: it wraps setTimeout/clearTimeout/
setInterval/clearInterval to count pending timers and the next fire time,
delegating to the originals with arguments untouched. It never delays,
accelerates, or cancels a page timer.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

TIMER_LEDGER_JS = r"""
(() => {
  if (window.__agent_timer_ledger) return true;
  const ledger = {
    installed: true,
    timeouts: new Map(),
    intervals: 0,
  };
  const origSetTimeout = window.setTimeout.bind(window);
  const origClearTimeout = window.clearTimeout.bind(window);
  const origSetInterval = window.setInterval.bind(window);
  const origClearInterval = window.clearInterval.bind(window);
  window.setTimeout = function (fn, delay, ...args) {
    const d = Number(delay) || 0;
    const id = origSetTimeout(function (...a) {
      ledger.timeouts.delete(id);
      return typeof fn === 'function' ? fn(...a) : undefined;
    }, d, ...args);
    ledger.timeouts.set(id, Date.now() + d);
    return id;
  };
  window.clearTimeout = function (id) {
    ledger.timeouts.delete(id);
    return origClearTimeout(id);
  };
  window.setInterval = function (fn, delay, ...args) {
    ledger.intervals += 1;
    return origSetInterval(fn, delay, ...args);
  };
  window.clearInterval = function (id) {
    ledger.intervals = Math.max(0, ledger.intervals - 1);
    return origClearInterval(id);
  };
  window.__agent_timer_ledger = ledger;
  return true;
})()
//# sourceURL=__agent_internal_timer_ledger__.js
"""

READ_LEDGER_JS = """
(() => {
  const l = window.__agent_timer_ledger;
  if (!l) return {installed: false, pending_timeouts: 0, pending_intervals: 0, next_fire_in_ms: null};
  const now = Date.now();
  let next = null;
  for (const fireAt of l.timeouts.values()) {
    const delta = fireAt - now;
    if (next === null || delta < next) next = delta;
  }
  return {installed: true, pending_timeouts: l.timeouts.size,
          pending_intervals: l.intervals, next_fire_in_ms: next};
})()
"""


def ledger_enabled_from_env() -> bool:
    return os.environ.get("AGENTBROWSER_TIMER_LEDGER", "").strip().lower() in ("1", "true", "yes", "on")


def parse_ledger(value: Any) -> Dict[str, Any]:
    """Normalize a raw ledger read into the settle decision shape."""
    if not isinstance(value, dict) or not value.get("installed"):
        return {"installed": False, "pending_timeouts": 0,
                "pending_intervals": 0, "next_fire_in_ms": None}
    return {
        "installed": True,
        "pending_timeouts": int(value.get("pending_timeouts") or 0),
        "pending_intervals": int(value.get("pending_intervals") or 0),
        "next_fire_in_ms": value.get("next_fire_in_ms"),
    }


def timers_block_settle(ledger: Dict[str, Any], quiet_window_ms: float = 250.0) -> bool:
    """
    True when a pending timeout fires within the quiet window: settling
    now would race that timer's work. Long-lived intervals never block
    (they are the page's heartbeat, reported but non-blocking).
    """
    if not ledger.get("installed"):
        return False
    if not ledger.get("pending_timeouts"):
        return False
    nxt = ledger.get("next_fire_in_ms")
    if nxt is None:
        return True
    return nxt <= quiet_window_ms
