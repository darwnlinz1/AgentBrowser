import json
import time
from typing import Dict, Any, List, Optional

from agent_browser.evidence import correlate_diagnostics


class DiagnosticEngine:
    """
    Automated Root Cause Diagnostics & Bug Reproduction Generator.
    Analyzes causal correlations between user actions, network failures, and runtime exceptions.
    """
    def __init__(self, dom_engine, network_engine, runtime_engine, storage_engine, tracer):
        self.dom = dom_engine
        self.network = network_engine
        self.runtime = runtime_engine
        self.storage = storage_engine
        self.tracer = tracer

    async def run_diagnostics(self) -> Dict[str, Any]:
        """
        Executes a deep diagnostic health check on the current session.
        """
        issues = []
        recommendations = []

        # 1. Check for uncaught runtime exceptions
        exceptions = self.runtime.get_exceptions(limit=10)
        for ex in exceptions:
            issues.append({
                "severity": "CRITICAL",
                "category": "RUNTIME_EXCEPTION",
                "message": ex.get("description") or ex.get("text"),
                "location": f"{ex.get('url')}:{ex.get('line')}:{ex.get('column')}",
                "stack": ex.get("stack")
            })

        # 2. Check for failing network requests (4xx, 5xx, or network failure).
        # Issue details stay narrow so a diagnostic dump does not print cookies.
        recent_requests = self.network.search_traffic(limit=30)
        failing_requests = []
        for r in recent_requests:
            status = r.get("status")
            narrow = {
                "id": r.get("id"),
                "method": r.get("method"),
                "url": r.get("url"),
                "status": status,
                "resource_type": r.get("resource_type"),
                "error_text": r.get("error_text"),
            }
            if status == "FAILED":
                failing_requests.append(r)
                issues.append({
                    "severity": "HIGH",
                    "category": "NETWORK_FAILURE",
                    "message": f"{r.get('method')} {r.get('url')} failed: {r.get('error_text')}",
                    "details": narrow,
                })
            elif isinstance(status, int) and status >= 400:
                failing_requests.append(r)
                issues.append({
                    "severity": "HIGH",
                    "category": "HTTP_ERROR",
                    "message": f"{r.get('method')} {r.get('url')} returned HTTP {status}",
                    "details": {
                        "status": status,
                        "url": r.get("url"),
                        "id": r.get("id"),
                        "resource_type": r.get("resource_type"),
                        "response_preview": str(r.get("response_body") or "")[:300],
                    },
                })

        # 3. Causal claims come only from evidence.correlate_diagnostics.
        # Correlate the same actionable set the trace uses: favicon, image,
        # and navigation errors such as ERR_UNSAFE_PORT stay visible as issues
        # and must not erase the single API-failure signal.
        hypotheses = correlate_diagnostics(exceptions, recent_requests)
        actionable = []
        for r in recent_requests:
            url = (r.get("url") or "").lower().split("?", 1)[0]
            rtype = (r.get("resource_type") or "").lower()
            status = r.get("status")
            failing = status == "FAILED" or (isinstance(status, int) and status >= 400)
            if not failing:
                continue
            if rtype in ("image", "font", "media", "stylesheet", "document"):
                continue
            if url.endswith("/favicon.ico") or url.endswith(".ico"):
                continue
            error_text = (r.get("error_text") or "").lower()
            if "err_unsafe_port" in error_text or "err_connection_refused" in error_text:
                continue
            actionable.append(r)
        if actionable:
            primary = actionable[0]
            recommendations.insert(0, (
                f"Primary actionable failure: {primary.get('method')} {primary.get('url')} "
                f"-> {primary.get('status')}. Static asset and favicon failures are listed "
                "separately and do not erase this signal."
            ))
        for hyp in hypotheses:
            if hyp.get("status") == "insufficient_evidence":
                recommendations.append(hyp["message"])
            elif hyp.get("confidence") == "supported":
                recommendations.append(
                    f"Supported signal ({hyp.get('kind')}): {hyp.get('message')}"
                )
            elif hyp.get("confidence") == "weak":
                recommendations.append(
                    f"Weak signal ({hyp.get('kind')}): {hyp.get('message')} "
                    "This is not proof that the action triggered the request."
                )

        # 4a. Structured browser issues from the CDP Audits domain (authoritative,
        # carries requestId/source locations) -- preferred over console text.
        audit_issues = []
        if hasattr(self.runtime, "get_audit_issues"):
            try:
                audit_issues = self.runtime.get_audit_issues(limit=20)
            except Exception:
                audit_issues = []
        seen_audit_categories = set()
        for ai in audit_issues:
            cat = ai.get("category") or "BROWSER_AUDIT"
            seen_audit_categories.add(cat)
            issues.append({
                "severity": "HIGH" if cat in ("CORS_POLICY", "MIXED_CONTENT") else "MEDIUM",
                "category": cat,
                "message": f"{ai.get('code') or cat} {ai.get('url') or ''}".strip(),
                "details": {"request_id": ai.get("request_id"), "url": ai.get("url"), "source": ai.get("source")},
            })
            if cat == "CORS_POLICY":
                recommendations.append("CORS blocked request (Audits). Inspect Access-Control-Allow-Origin / preflight on the server.")

        # 4b. Console-error fallback only for categories Audits did not report.
        console_errors = self.runtime.get_console_messages(level="error", limit=10)
        for ce in console_errors:
            text = ce.get("text", "")
            if any(term in text.lower() for term in ("cors", "access-control-allow-origin")):
                if "CORS_POLICY" in seen_audit_categories:
                    continue
                issues.append({
                    "severity": "HIGH",
                    "category": "CORS_POLICY",
                    "message": text
                })
                recommendations.append("CORS blocked request. Inspect server Access-Control-Allow-Origin headers or use agent_browser interceptor to tamper origin.")
            elif "content security policy" in text.lower():
                if "CSP_VIOLATION" in seen_audit_categories:
                    continue
                issues.append({
                    "severity": "MEDIUM",
                    "category": "CSP_VIOLATION",
                    "message": text
                })

        snapshot = await self.dom.get_interactive_snapshot()

        return {
            "timestamp": time.time(),
            "url": snapshot.get("url"),
            "title": snapshot.get("title"),
            "health": "UNHEALTHY" if issues else "HEALTHY",
            "total_issues": len(issues),
            "audit_issues_count": len(audit_issues),
            "issues": issues,
            "hypotheses": hypotheses,
            "root_cause_analysis": recommendations or ["No automated root-cause patterns detected. System is running nominally."]
        }

    async def generate_reproduction_script(self, target_url: str, failing_action: Dict[str, Any]) -> str:
        """
        Generates a standalone, reproducible Python test script for the detected bug.
        """
        action_type = failing_action.get("type", "click")
        selector = failing_action.get("target", "")
        url_lit = json.dumps(target_url)
        action_lit = json.dumps(action_type)
        selector_lit = json.dumps(selector)

        script = f'''#!/usr/bin/env python3
"""
Automated Bug Reproduction Script
Generated by AgentBrowser Platform
"""

import asyncio
from agent_browser.agent_api import AgentBrowserController

TARGET_URL = {url_lit}
ACTION = {action_lit}
selector = {selector_lit}
SELECTOR = selector

async def reproduce():
    from agent_browser.session_registry import find_free_port
    controller = AgentBrowserController(port=find_free_port(), headless=True, profile='ab-repro')
    await controller.start()
    try:
        print("[*] Navigating...")
        await controller.navigate(TARGET_URL)

        print("[*] Executing action...")
        trace = await controller.trace_action(ACTION, target=SELECTOR)

        print("[*] Trace result:")
        _net = trace.get('network', {{}})
        _run = trace.get('runtime', {{}})
        print(f"    Success: {{trace.get('action', {{}}).get('success')}}")
        print(f"    Requests triggered (supported/attributed): {{_net.get('requests_triggered_count', 0)}}")
        print(f"    Requests observed: {{_net.get('requests_observed_count', 0)}}")
        print(f"    Failing requests: {{_net.get('failing_requests_count', 0)}}")
        print(f"    Runtime exceptions: {{_run.get('exceptions_count', 0)}}")

        if _run.get('exceptions_count', 0) > 0 or _net.get('failing_requests_count', 0) > 0:
            print("[!] BUG CONFIRMED REPRODUCED!")
            return False
        else:
            print("[+] Action executed without errors.")
            return True
    finally:
        await controller.stop()

if __name__ == "__main__":
    success = asyncio.run(reproduce())
    exit(0 if success else 1)
'''
        return script

    @staticmethod
    def failure_signature(exceptions: List[Dict[str, Any]], requests: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Normalized oracle for a bug: exception class + message template (digits
        collapsed) and the first actionable failing request's path template +
        status. Background noise cannot satisfy this signature on its own.
        """
        from agent_browser.evidence import normalize_exception_message, url_path_template, url_template

        exc = (exceptions or [{}])[0] if exceptions else {}
        desc = exc.get("description") or exc.get("text") or ""
        exc_class = desc.split(":", 1)[0].strip() if desc else None
        normalized = normalize_exception_message(desc)
        failing = None
        for r in requests or []:
            status = r.get("status")
            if status == "FAILED" or (isinstance(status, int) and status >= 400):
                failing = {"path": url_template(r.get("url")), "status": status,
                           "path_only": url_path_template(r.get("url"))}
                break
        return {
            "exception_class": exc_class,
            "exception_template": normalized["template"],
            "exception_template_id": normalized["template_id"],
            "failing_request": failing,
        }

    async def generate_regression_test(
        self,
        target_url: str,
        failing_action: Dict[str, Any],
        signature: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Emit a pytest regression test whose pass/fail is the failure signature
        (failing request path+status AND exception class), not 'any error'.
        Locators use the caller-supplied stable selector (CSS/role/text), never
        a snapshot-only ``#<ref>``.
        """
        action_type = failing_action.get("type", "click")
        selector = failing_action.get("target", "")
        signature = signature or {}
        failing = signature.get("failing_request") or {}
        lines = [
            "#!/usr/bin/env python3",
            '"""Auto-generated regression test (failure-signature oracle)."""',
            "import asyncio",
            "from agent_browser.agent_api import AgentBrowserController",
            "",
            f"TARGET_URL = {json.dumps(target_url)}",
            f"ACTION = {json.dumps(action_type)}",
            f"SELECTOR = {json.dumps(selector)}",
            f"EXPECTED_FAIL_PATH = {json.dumps(failing.get('path'))}",
            f"EXPECTED_FAIL_STATUS = {json.dumps(failing.get('status'))}",
            f"EXPECTED_EXCEPTION = {json.dumps(signature.get('exception_class'))}",
            "",
            "async def _run():",
            "    from agent_browser.session_registry import find_free_port",
            "    c = AgentBrowserController(port=find_free_port(), headless=True, profile='ab-repro')",
            "    await c.start()",
            "    try:",
            "        await c.navigate(TARGET_URL)",
            "        trace = await c.trace_action(ACTION, target=SELECTOR)",
            "        failing = [r for r in trace['network']['requests'] if isinstance(r.get('status'), int) and r['status'] >= 400]",
            "        path_ok = EXPECTED_FAIL_PATH is None or any(EXPECTED_FAIL_PATH.split('{n}')[0] in (r.get('url') or '') for r in failing)",
            "        status_ok = EXPECTED_FAIL_STATUS is None or any(r.get('status') == EXPECTED_FAIL_STATUS for r in failing)",
            "        exc_ok = EXPECTED_EXCEPTION is None or any(EXPECTED_EXCEPTION in str(e.get('description') or e.get('text') or '') for e in trace['runtime']['exceptions'])",
            "        return bool(failing) and path_ok and status_ok and exc_ok",
            "    finally:",
            "        await c.stop()",
            "",
            "def test_bug_reproduces():",
            "    # Fails (bug present) is the expected state before the fix; after the",
            "    # fix this test should be inverted to assert not reproduced.",
            "    assert asyncio.run(_run()), 'failure signature not reproduced'",
            "",
        ]
        return "\n".join(lines)
