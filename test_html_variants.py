import requests
import re
from shop_link_parser import COMMON_HEADERS

r = requests.get('https://flamingoestate.com/products/heritage-extra-virgin-olive-oil', headers=COMMON_HEADERS)
print('status:', r.status_code)

# Check meta or script tags
matches = re.findall(r'(\d{14})', r.text)
print('Long numeric IDs:', set(matches))

# Check for variant in HTML forms or scripts
form_variants = re.findall(r'name=["\']id["\']\s+value=["\'](\d+)["\']', r.text)
print('Form id values:', form_variants)

script_variants = re.findall(r'"variants"\s*:\s*(\[[^\]]+\])', r.text)
print('Script variants count:', len(script_variants))
