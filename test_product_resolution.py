import requests
import json
import re

with open("paneer_live_cookies.json", "r") as f:
    cookies = json.load(f)
c_dict = {c["name"]: c["value"] for c in cookies}

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Not:A-Brand";v="99", "Chromium";v="155", "Google Chrome";v="155"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}

# Test 1: Direct merchant link
merchant_url = "https://flamingoestate.com/products/heritage-extra-virgin-olive-oil"
print(f"[*] Testing Direct Merchant URL: {merchant_url}")
# On any Shopify merchant, appending .js or .json returns full product data & all variant IDs!
js_url = "https://flamingoestate.com/products/heritage-extra-virgin-olive-oil.js"
r = requests.get(js_url, headers=headers)
print("  .js status:", r.status_code)
if r.status_code == 200:
    prod = r.json()
    print(f"  Title: {prod.get('title')}")
    variants = prod.get("variants", [])
    print(f"  Total Variants: {len(variants)}")
    for v in variants[:3]:
        print(f"    - ID: {v.get('id')} | Title: {v.get('title')} | Price: {v.get('price')} | Available: {v.get('available')}")

# Test 2: Shop App product URL
shop_url = "https://shop.com/products/10138865172772/mens-cargo-bib-short-black"
print(f"\n[*] Testing Shop.com product URL: {shop_url}")
r_shop = requests.get(shop_url, headers=headers, cookies=c_dict)
print("  Shop.com status:", r_shop.status_code)
if r_shop.status_code == 200:
    # Look for canonical merchant URL in HTML
    canonical = re.findall(r'<link rel="canonical" href="([^"]+)"', r_shop.text)
    print("  Canonical:", canonical)
    # Search for merchant domain or external product URL in script tags / remix state
    external_links = re.findall(r'https?://[a-zA-Z0-9\-\.]+/products/[a-zA-Z0-9\-_]+', r_shop.text)
    print("  Found product links in HTML:", set(external_links))
