import json
from typing import Dict, Any, List, Optional

from agent_browser.evidence import cookie_url_for_domain


class StorageEngine:
    """
    Storage & Session State Manager.
    Inspects, modifies, exports, and imports Cookies, LocalStorage, and SessionStorage.
    """
    def __init__(self, cdp_conn, active_session_id_getter):
        self.cdp = cdp_conn
        self.get_session_id = active_session_id_getter

    async def get_cookies(self, urls: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        sid = self.get_session_id()
        params = {"urls": urls} if urls else {}
        res = await self.cdp.send("Network.getCookies", params, session_id=sid)
        return res.get("cookies", res.get("result", {}).get("cookies", []))

    async def set_cookie(
        self,
        name: str,
        value: str,
        domain: Optional[str] = None,
        path: str = "/",
        secure: bool = False,
        http_only: bool = False,
        same_site: Optional[str] = None,
        expires: Optional[float] = None,
        url: Optional[str] = None
    ) -> bool:
        sid = self.get_session_id()
        params: Dict[str, Any] = {
            "name": name,
            "value": value,
            "path": path,
            "secure": secure,
            "httpOnly": http_only
        }
        if url:
            params["url"] = url
        elif domain:
            host = domain.split(":")[0]
            if host in ("127.0.0.1", "localhost") or host.replace(".", "").isdigit():
                params["url"] = cookie_url_for_domain(domain, path)
            else:
                params["domain"] = domain

        if same_site:
            params["sameSite"] = same_site
        if expires:
            params["expires"] = expires

        res = await self.cdp.send("Network.setCookie", params, session_id=sid)
        return res.get("success", res.get("result", {}).get("success", True))

    async def delete_cookies(self, name: str, domain: Optional[str] = None, path: Optional[str] = None) -> bool:
        sid = self.get_session_id()
        params: Dict[str, Any] = {"name": name}
        if domain:
            params["domain"] = domain
        if path:
            params["path"] = path
        await self.cdp.send("Network.deleteCookies", params, session_id=sid)
        return True

    async def clear_all_cookies(self) -> bool:
        sid = self.get_session_id()
        await self.cdp.send("Network.clearBrowserCookies", {}, session_id=sid)
        return True

    async def get_storage(self, storage_type: str = "local") -> Dict[str, str]:
        sid = self.get_session_id()
        prop = "localStorage" if storage_type == "local" else "sessionStorage"
        expr = f"""
        (() => {{
            const res = {{}};
            try {{
                for (let i = 0; i < {prop}.length; i++) {{
                    const k = {prop}.key(i);
                    res[k] = {prop}.getItem(k);
                }}
            }} catch(e) {{
                res["__error__"] = String(e);
            }}
            return res;
        }})()
        """
        res = await self.cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session_id=sid)
        return res.get("result", {}).get("value", {})

    async def set_storage_item(self, storage_type: str, key: str, value: str) -> bool:
        sid = self.get_session_id()
        prop = "localStorage" if storage_type == "local" else "sessionStorage"
        expr = f"""
        (() => {{
            {prop}.setItem({json.dumps(key)}, {json.dumps(value)});
            return true;
        }})()
        """
        await self.cdp.send("Runtime.evaluate", {"expression": expr}, session_id=sid)
        return True

    async def remove_storage_item(self, storage_type: str, key: str) -> bool:
        sid = self.get_session_id()
        prop = "localStorage" if storage_type == "local" else "sessionStorage"
        expr = f"""
        (() => {{
            {prop}.removeItem({json.dumps(key)});
            return true;
        }})()
        """
        await self.cdp.send("Runtime.evaluate", {"expression": expr}, session_id=sid)
        return True

    async def clear_storage(self, storage_type: str) -> bool:
        sid = self.get_session_id()
        prop = "localStorage" if storage_type == "local" else "sessionStorage"
        expr = f"""
        (() => {{
            {prop}.clear();
            return true;
        }})()
        """
        await self.cdp.send("Runtime.evaluate", {"expression": expr}, session_id=sid)
        return True

    async def export_session_state(self) -> Dict[str, Any]:
        """
        Exports full state (cookies + localStorage + sessionStorage) for bug reproduction.
        """
        cookies = await self.get_cookies()
        local_store = await self.get_storage("local")
        session_store = await self.get_storage("session")
        return {
            "cookies": cookies,
            "localStorage": local_store,
            "sessionStorage": session_store
        }

    async def import_session_state(self, state: Dict[str, Any]) -> bool:
        """
        Restores full state from an exported snapshot.
        """
        cookies = state.get("cookies", [])
        for c in cookies:
            await self.set_cookie(
                name=c["name"],
                value=c["value"],
                domain=c.get("domain"),
                path=c.get("path", "/"),
                secure=c.get("secure", False),
                http_only=c.get("httpOnly", False),
                same_site=c.get("sameSite")
            )

        for k, v in state.get("localStorage", {}).items():
            if not k.startswith("__"):
                await self.set_storage_item("local", k, v)

        for k, v in state.get("sessionStorage", {}).items():
            if not k.startswith("__"):
                await self.set_storage_item("session", k, v)

        return True
