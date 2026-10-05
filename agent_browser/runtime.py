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
        self.contexts: Dict[int, Dict[str, Any]] = {}
        self._max_logs = 2000

    async def enable(self):
        sid = self.get_session_id()
        await self.cdp.send("Runtime.enable", {}, session_id=sid)
        await self.cdp.send("Log.enable", {}, session_id=sid)

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
            return {
                "success": False,
                "error": exception_details.get("text"),
                "exception": exception_details.get("exception", {}).get("description")
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

    def clear_logs(self):
        self.console_logs.clear()
        self.exceptions.clear()
