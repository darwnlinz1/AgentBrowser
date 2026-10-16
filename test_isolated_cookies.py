import asyncio
import json
from agent_browser.agent_api import AgentBrowserController

async def test():
    with open('extracted_default_cookies.json', 'r', encoding='utf-8') as f:
        cookies = json.load(f)
    ctrl = AgentBrowserController(port=9247, headless=True)
    await ctrl.start()
    sid = ctrl.get_active_session_id()
    for c in cookies:
        dom = c['host']
        clean_dom = dom.lstrip('.')
        scheme = 'https' if c['secure'] else 'http'
        cdp_c = {
            'name': c['name'],
            'value': c['value'],
            'url': f"{scheme}://{clean_dom}{c['path']}",
            'domain': dom,
            'path': c['path'],
            'secure': bool(c['secure']),
            'httpOnly': bool(c['httpOnly'])
        }
        if c.get('sameSite') == 1:
            cdp_c['sameSite'] = 'Lax'
        elif c.get('sameSite') == 2:
            cdp_c['sameSite'] = 'Strict'
        elif c.get('sameSite') == 0 and c['secure']:
            cdp_c['sameSite'] = 'None'
        try:
            await ctrl.cdp.send('Network.setCookies', {'cookies': [cdp_c]}, session_id=sid)
            print(f"[+] OK: {c['host']} {c['name']}")
        except Exception as e:
            print(f"[-] FAILED: {c['host']} {c['name']}: {e}")
    await ctrl.stop()

if __name__ == '__main__':
    asyncio.run(test())
