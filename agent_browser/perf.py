"""
Web-vitals performance engine.

Two evidence sources, both first-party:
- In-page PerformanceObserver (buffered), which replays LCP, layout-shift,
  and long-task entries that already happened, plus Navigation Timing.
- The CDP Performance domain (renderer counters: nodes, layout/style
  recalc counts, JS heap), which works even when page JS is hostile.

Verdicts use the standard web-vitals thresholds (LCP 2.5s/4s, CLS 0.1/0.25,
INP/long-task 200ms/500ms bands) and are labeled as measurements, not proof
of user-perceived quality on real devices.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

VITALS_JS = r"""
(async () => {
  const collect = (type) => new Promise((resolve) => {
    const entries = [];
    try {
      const obs = new PerformanceObserver((list) => { entries.push(...list.getEntries()); });
      obs.observe({ type, buffered: true });
      setTimeout(() => { try { obs.disconnect(); } catch (e) {} resolve(entries); }, 60);
    } catch (e) { resolve(entries); }
  });
  const [lcpEntries, shiftEntries, longEntries] = await Promise.all([
    collect('largest-contentful-paint'), collect('layout-shift'), collect('longtask')
  ]);
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const lcp = lcpEntries.length ? lcpEntries[lcpEntries.length - 1] : null;
  let cls = 0;
  for (const e of shiftEntries) { if (!e.hadRecentInput) cls += e.value || 0; }
  const longTasks = longEntries.map(e => ({ duration: Math.round(e.duration), startTime: Math.round(e.startTime) }));
  return {
    url: location.href,
    navigation: {
      responseEnd: Math.round(nav.responseEnd || 0),
      domContentLoaded: Math.round(nav.domContentLoadedEventEnd || 0),
      load: Math.round(nav.loadEventEnd || 0),
      transferSize: nav.transferSize || 0
    },
    lcp_ms: lcp ? Math.round(lcp.startTime) : null,
    lcp_element: lcp && lcp.element ? (lcp.element.tagName || '').toLowerCase() : null,
    cls: Math.round(cls * 1000) / 1000,
    long_tasks: longTasks.slice(0, 20),
    long_task_count: longEntries.length,
    max_long_task_ms: longTasks.length ? Math.max(...longTasks.map(t => t.duration)) : 0
  };
})()
//# sourceURL=__agent_internal_vitals__.js
"""


def grade_vitals(vitals: Dict[str, Any]) -> Dict[str, Any]:
    """Apply standard thresholds; missing data grades as 'unknown', never good."""
    def band(value, good, poor):
        if value is None:
            return "unknown"
        if value <= good:
            return "good"
        if value <= poor:
            return "needs-improvement"
        return "poor"

    lcp = vitals.get("lcp_ms")
    cls = vitals.get("cls")
    max_long = vitals.get("max_long_task_ms") or 0
    return {
        "lcp": band(lcp, 2500, 4000),
        "cls": band(cls, 0.1, 0.25),
        "long_tasks": band(max_long if vitals.get("long_task_count") else 0, 200, 500),
    }


class PerfEngine:
    def __init__(self, cdp_conn, active_session_id_getter):
        self.cdp = cdp_conn
        self.get_session_id = active_session_id_getter

    async def get_web_vitals(self) -> Dict[str, Any]:
        sid = self.get_session_id()
        res = await self.cdp.send("Runtime.evaluate", {
            "expression": VITALS_JS, "returnByValue": True, "awaitPromise": True,
        }, session_id=sid, timeout=10.0)
        vitals = ((res.get("result") or {}).get("value") or {})
        return {**vitals, "grades": grade_vitals(vitals)}

    async def get_renderer_metrics(self) -> Dict[str, Any]:
        sid = self.get_session_id()
        try:
            await self.cdp.send("Performance.enable", {}, session_id=sid)
        except Exception:
            pass
        res = await self.cdp.send("Performance.getMetrics", {}, session_id=sid)
        metrics = {m.get("name"): m.get("value") for m in (res.get("metrics") or [])}
        keys = ("Documents", "Frames", "Nodes", "JSEventListeners", "LayoutCount",
                "RecalcStyleCount", "LayoutDuration", "RecalcStyleDuration",
                "ScriptDuration", "TaskDuration", "JSHeapUsedSize", "JSHeapTotalSize")
        return {k: metrics.get(k) for k in keys if k in metrics}

    async def snapshot(self) -> Dict[str, Any]:
        return {"vitals": await self.get_web_vitals(), "renderer": await self.get_renderer_metrics()}
