import requests
import json

merchant = "flamingoestate.com"
r = requests.get(f"https://{merchant}/products.json?limit=1", headers={"User-Agent": "Mozilla/5.0"})
prod = r.json()["products"][0]
variant_id = prod["variants"][0]["id"]
print(f"Product: {prod['title']}, Variant ID: {variant_id}")

s = requests.Session()
s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

# Step 1: Add to cart
add_res = s.post(f"https://{merchant}/cart/add.js", json={"items": [{"id": variant_id, "quantity": 1}]})
print("Cart add status:", add_res.status_code)
cart_cookie = s.cookies.get("cart")
print("Cart Cookie:", cart_cookie)

# Step 2: Hit /checkout to get valid checkout URL
chk_res = s.get(f"https://{merchant}/checkout", allow_redirects=False)
print("Checkout 302 status:", chk_res.status_code)
checkout_url = chk_res.headers.get("Location")
print("Generated Checkout Link:", checkout_url)
