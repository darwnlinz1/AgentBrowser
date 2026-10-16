import os
import sys
import json
import asyncio
import requests
from agent_browser.agent_api import AgentBrowserController

def generate_valid_checkout_link(merchant_domain: str, variant_id: int) -> str:
    print(f"[*] Step 1: Generating checkout link via API for merchant '{merchant_domain}'...")
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
    })
    
    # 1. Add item to cart
    add_res = s.post(f"https://{merchant_domain}/cart/add.js", json={"items": [{"id": variant_id, "quantity": 1}]})
    if add_res.status_code not in (200, 201):
        raise RuntimeError(f"Cart add failed: {add_res.status_code}")
    print("[+] Item added to cart via pure API!")

    # 2. Hit checkout to get 302 Shop Pay link with tokens
    chk_res = s.get(f"https://{merchant_domain}/checkout", allow_redirects=False)
    checkout_url = chk_res.headers.get("Location")
    if not checkout_url:
        raise RuntimeError("No redirect Location returned from /checkout")
    print(f"[+] Successfully generated valid Shop Pay checkout link:\n    {checkout_url[:120]}...")
    return checkout_url

async def run_e2e_verification():
    print("=" * 70)
    print("  VERIFYING PANEER'S WORKFLOW: API LINK GENERATION + COOKIE BROWSER")
    print("=" * 70)

    merchant = "flamingoestate.com"
    # Get a real variant
    r = requests.get(f"https://{merchant}/products.json?limit=1", headers={"User-Agent": "Mozilla/5.0"})
    variant_id = r.json()["products"][0]["variants"][0]["id"]
    prod_name = r.json()["products"][0]["title"]
    print(f"[*] Target Product: {prod_name} (Variant: {variant_id})")

    # Step 1: Generate checkout link via pure API
    checkout_url = generate_valid_checkout_link(merchant, variant_id)

    # Step 2: Open secondary browser with account cookies
    profile_dir = os.path.abspath("profiles/e2e_checkout_proof")
    with open("real_plaintext_cookies.json", "r", encoding="utf-8") as f:
        cookies = json.load(f)

    ctrl = AgentBrowserController(port=9255, headless=True, profile_dir=profile_dir)
    await ctrl.start()

    try:
        sid = ctrl.get_active_session_id()
        # Inject cookies
        cdp_cookies = []
        for c in cookies:
            c_obj = {
                "name": c["name"],
                "value": c["value"],
                "domain": c["domain"],
                "path": c["path"],
                "secure": c.get("secure", False),
                "httpOnly": c.get("httpOnly", False)
            }
            if c.get("sameSite") in ("Lax", "Strict", "None"):
                c_obj["sameSite"] = c["sameSite"]
            cdp_cookies.append(c_obj)

        await ctrl.cdp.send("Network.setCookies", {"cookies": cdp_cookies}, session_id=sid)
        print(f"[+] Injected {len(cdp_cookies)} cookies into secondary browser!")

        # Step 3: Navigate to the generated checkout link
        print("[*] Navigating to generated checkout link...")
        await ctrl.navigate(checkout_url)
        await asyncio.sleep(4.0)

        # Check final URL and title
        res_url = await ctrl.runtime.evaluate("window.location.href")
        final_url = res_url.get("value", "")
        res_title = await ctrl.runtime.evaluate("document.title")
        final_title = res_title.get("value", "")

        print("\n" + "=" * 70)
        print(f"[+] Final URL: {final_url}")
        print(f"[+] Final Title: {final_title}")
        
        is_checkout_success = "shoppay" in final_url and "shoppay_login" not in final_url
        print(f"[+] Checkout Form Reached Without Re-login: {is_checkout_success}")
        print("=" * 70)
        return is_checkout_success

    finally:
        await ctrl.stop()

if __name__ == "__main__":
    success = asyncio.run(run_e2e_verification())
    sys.exit(0 if success else 1)
