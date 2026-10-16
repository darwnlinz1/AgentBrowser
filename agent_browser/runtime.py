import time
import json
from typing import Dict, Any, List, Optional


class RuntimeEngine:
    """
    JavaScript Execution, Console Logging, and Runtime Error Monitoring Engine.
    """
    def __init__(self, cdp_conn, active_session_id_getter):
        self.cdp = cdp_conn
        self.get_session_id = active_session_id_getter
        self.console_logs: List[Dict[str, Any]] = []
        self.exceptions: List[Dict[str, Any]] = []
        self.audit_issues: List[Dict[str, Any]] = []
        self.contexts: Dict[int, Dict[str, Any]] = {}
        self._max_logs = 2000

    async def enable(self, session_id: Optional[str] = None):
        sid = session_id or self.get_session_id()
        await self.cdp.send("Runtime.enable", {}, session_id=sid)
        await self.cdp.send("Log.enable", {}, session_id=sid)
        try:
            await self.cdp.send("Audits.enable", {}, session_id=sid)
        except Exception:
            pass  # Audits is best-effort; console fallback still applies.

    async def handle_cdp_event(self, data: Dict[str, Any]):
        method = data.get("method", "")
        params = data.get("params", {})

        # Console API Called (console.log, console.error, console.warn)
        if method == "Runtime.consoleAPICalled":
            msg_type = params.get("type", "log")
            args = params.get("args", [])
            rendered_args = []
            for a in args:
                if "value" in a:
                    rendered_args.append(str(a["value"]))
                elif "description" in a:
                    rendered_args.append(str(a["description"]))
                else:
                    rendered_args.append(json.dumps(a))

            entry = {
                "type": msg_type,
                "text": " ".join(rendered_args),
                "timestamp": params.get("timestamp", time.time()),
                "stack": params.get("stackTrace", {}).get("callFrames", [])
            }
            self.console_logs.append(entry)
            if len(self.console_logs) > self._max_logs:
                self.console_logs.pop(0)

        # Uncaught Exception Thrown
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            exc = details.get("exception", {})
            entry = {
                "text": details.get("text"),
                "description": exc.get("description", details.get("text")),
                "class_name": exc.get("className"),
                "script_id": details.get("scriptId"),
                "exception_id": details.get("exceptionId"),
                "origin": "rejection" if (details.get("text") or "").startswith("Uncaught (in promise)") else "uncaught",
                "url": details.get("url"),
                "line": details.get("lineNumber"),
                "column": details.get("columnNumber"),
                "stack": details.get("stackTrace", {}).get("callFrames", []),
                "timestamp": params.get("timestamp", time.time())
            }
            self.exceptions.append(entry)
            if len(self.exceptions) > 500:
                self.exceptions.pop(0)

        # Execution Context Created
        elif method == "Runtime.executionContextCreated":
            ctx = params.get("context", {})
            self.contexts[ctx.get("id")] = ctx

        # Execution Context Destroyed
        elif method == "Runtime.executionContextDestroyed":
            ctx_id = params.get("executionContextId")
            self.contexts.pop(ctx_id, None)

        # Execution Contexts Cleared
        elif method == "Runtime.executionContextsCleared":
            self.contexts.clear()

        elif method == "Log.entryAdded":
            entry_in = params.get("entry") or {}
            level = (entry_in.get("level") or "log").lower()
            entry = {
                "type": level,
                "text": entry_in.get("text") or "",
                "timestamp": entry_in.get("timestamp", time.time()),
                "source": entry_in.get("source"),
                "url": entry_in.get("url"),
                "stack": (entry_in.get("stackTrace") or {}).get("callFrames", []),
            }
            self.console_logs.append(entry)
            if len(self.console_logs) > self._max_logs:
                self.console_logs.pop(0)

        # Browser-detected issues (CORS/CSP/cookie/mixed-content) with structure.
        elif method == "Audits.issueAdded":
            issue = params.get("issue") or {}
            code = str(issue.get("code") or "")
            details = issue.get("details") or {}
            category = "BROWSER_AUDIT"
            request_id = None
            url = None
            for key in ("corsIssueDetails", "contentSecurityPolicyIssueDetails", "cookieIssueDetails", "mixedContentIssueDetails"):
                sub = details.get(key) or {}
                if not sub:
                    continue
                if key == "corsIssueDetails":
                    category = "CORS_POLICY"
                    request_id = sub.get("request", {}).get("requestId") if isinstance(sub.get("request"), dict) else None
                    url = (sub.get("request") or {}).get("url") if isinstance(sub.get("request"), dict) else sub.get("location", {}).get("url") if isinstance(sub.get("location"), dict) else None
                elif key == "contentSecurityPolicyIssueDetails":
                    category = "CSP_VIOLATION"
                    url = sub.get("blockedURL")
                elif key == "cookieIssueDetails":
                    category = "COOKIE_ISSUE"
                    url = (sub.get("cookie") or {}).get("domain") if isinstance(sub.get("cookie"), dict) else None
                elif key == "mixedContentIssueDetails":
                    category = "MIXED_CONTENT"
                    url = sub.get("insecureURL")
            entry = {
                "code": code,
                "category": category,
                "details": details,
                "request_id": request_id,
                "url": url,
                "timestamp": time.time(),
                "source": "Audits.issueAdded",
            }
            self.audit_issues.append(entry)
            if len(self.audit_issues) > 500:
                self.audit_issues.pop(0)

    async def evaluate(self, expression: str, context_id: Optional[int] = None, timeout: float = 10.0) -> Dict[str, Any]:
        """
        Executes JavaScript expression safely in the active page or worker context.
        """
        sid = self.get_session_id()
        payload: Dict[str, Any] = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "userGesture": True
        }
        if context_id is not None:
            payload["contextId"] = context_id

        res = await self.cdp.send("Runtime.evaluate", payload, session_id=sid, timeout=timeout)
        result_obj = res.get("result", {})
        exception_details = res.get("exceptionDetails")

        if exception_details:
            description = exception_details.get("exception", {}).get("description") or exception_details.get("text")
            self.exceptions.append({
                "text": exception_details.get("text"),
                "description": description,
                "class_name": (exception_details.get("exception") or {}).get("className"),
                "script_id": exception_details.get("scriptId"),
                "exception_id": exception_details.get("exceptionId"),
                "origin": "evaluate",
                "url": exception_details.get("url"),
                "line": exception_details.get("lineNumber"),
                "column": exception_details.get("columnNumber"),
                "stack": (exception_details.get("stackTrace") or {}).get("callFrames", []),
                "timestamp": time.time(),
                "source": "Runtime.evaluate",
            })
            if len(self.exceptions) > 500:
                self.exceptions.pop(0)
            return {
                "success": False,
                "error": exception_details.get("text"),
                "exception": description,
            }

        return {
            "success": True,
            "type": result_obj.get("type"),
            "value": result_obj.get("value")
        }

    def get_console_messages(self, level: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        if not level:
            return self.console_logs[-limit:]
        return [l for l in self.console_logs if l["type"].lower() == level.lower()][-limit:]

    def get_exceptions(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self.exceptions[-limit:]

    def get_audit_issues(self, category: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        items = self.audit_issues
        if category:
            items = [i for i in items if i.get("category") == category]
        return items[-limit:]

    def clear_logs(self):
        self.console_logs.clear()
        self.exceptions.clear()
