import unittest
import os
import json
from shop_link_parser import ShopLinkParser
from proxy_utils import ProxyParser
from cookie_utils import CookieManager
from checkout_generator import CheckoutGenerator

class TestShopToolSuite(unittest.TestCase):
    def test_proxy_parser_no_auth(self):
        p = ProxyParser.parse("1.2.3.4:8080")
        self.assertIsNotNone(p)
        self.assertEqual(p["http"], "http://1.2.3.4:8080")
        self.assertEqual(p["https"], "http://1.2.3.4:8080")

    def test_proxy_parser_with_auth(self):
        p = ProxyParser.parse("1.2.3.4:8080:myuser:mypass")
        self.assertIsNotNone(p)
        self.assertEqual(p["http"], "http://myuser:mypass@1.2.3.4:8080")
        self.assertEqual(p["https"], "http://myuser:mypass@1.2.3.4:8080")

    def test_proxy_parser_socks5(self):
        p = ProxyParser.parse("socks5://1.2.3.4:1080")
        self.assertIsNotNone(p)
        self.assertEqual(p["http"], "socks5://1.2.3.4:1080")

    def test_cookie_loader_json(self):
        cookies = CookieManager.load_cookie_file("paneer_live_cookies.json")
        self.assertGreater(len(cookies), 0)
        names = [c["name"] for c in cookies]
        self.assertIn("_shopify_essential", names)
        self.assertIn("_shop_app_essential", names)

    def test_link_parser_direct_merchant(self):
        url = "https://www.ornotbike.com/products/cargo-bib-short-black"
        res = ShopLinkParser.parse_url(url)
        self.assertEqual(res["merchant_domain"], "www.ornotbike.com")
        self.assertGreater(len(res["variants"]), 0)
        self.assertIsNotNone(res["selected_variant_id"])
        print(f"[+] Parsed {res['product_title']}: Variant ID = {res['selected_variant_id']}")

    def test_link_parser_explicit_variant(self):
        url = "https://www.ornotbike.com/products/cargo-bib-short-black?variant=50907075412260"
        res = ShopLinkParser.parse_url(url)
        self.assertEqual(res["selected_variant_id"], 50907075412260)

    def test_link_parser_shop_p(self):
        url = "https://shop.com/p/10138869137700/50907088224548?utm_source=shop_app"
        res = ShopLinkParser.parse_url(url)
        self.assertEqual(res["selected_variant_id"], 50907088224548)

    def test_end_to_end_generator(self):
        gen = CheckoutGenerator(proxy_str=None)
        count = gen.load_cookies("paneer_live_cookies.json")
        self.assertGreater(count, 0)
        
        # Test generation with real store and variant
        res = gen.generate(merchant_domain="flamingoestate.com", variant_id=41551252947112, quantity=1)
        self.assertTrue(res["success"])
        self.assertIn("checkout", res["checkout_url"])
        print(f"[+] End-to-end checkout URL generated:\n{res['checkout_url'][:100]}...")

if __name__ == "__main__":
    unittest.main()
