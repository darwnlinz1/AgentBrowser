import os
import sys
import json
import base64
import sqlite3
import time
import asyncio
import win32crypt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

def extract_cookies_from_profile(profile_dir):
    local_state_path = os.path.join(profile_dir, "Local State")
    cookies_db_path = os.path.join(profile_dir, "Default", "Network", "Cookies")
    
    if not os.path.exists(local_state_path) or not os.path.exists(cookies_db_path):
        raise FileNotFoundError(f"Missing Local State or Cookies DB in {profile_dir}")
        
    with open(local_state_path, "r", encoding="utf-8") as f:
        local_state = json.load(f)
        
    encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])[5:]
    master_key = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]
    
    conn = sqlite3.connect(cookies_db_path)
    cur = conn.cursor()
    cur.execute("""
        SELECT host_key, name, path, is_secure, is_httponly, samesite, expires_utc, encrypted_value 
        FROM cookies
    """)
    
    samesite_map = {-1: "Unspecified", 0: "None", 1: "Lax", 2: "Strict"}
    cookies = []
    
    for host, name, path, sec, httponly, samesite, expires_utc, enc_val in cur.fetchall():
        if enc_val.startswith(b"v10") or enc_val.startswith(b"v11"):
            nonce = enc_val[3:15]
            ciphertext = enc_val[15:]
            aesgcm = AESGCM(master_key)
            raw = aesgcm.decrypt(nonce, ciphertext, None)
            if len(raw) > 32:
                val = raw[32:].decode("utf-8", errors="ignore")
            else:
                val = raw.decode("utf-8", errors="ignore")
        else:
            raw = win32crypt.CryptUnprotectData(enc_val, None, None, None, 0)[1]
            val = raw.decode("utf-8", errors="ignore")
            
        cookie_obj = {
            "name": name,
            "value": val,
            "domain": host,
            "path": path,
            "secure": bool(sec),
            "httpOnly": bool(httponly),
            "sameSite": samesite_map.get(samesite, "None")
        }
        if expires_utc > 0:
            # Convert Chromium timestamp (microseconds since 1601) to UNIX timestamp
            unix_exp = (expires_utc / 1000000.0) - 11644473600.0
            cookie_obj["expires"] = unix_exp
            
        cookies.append(cookie_obj)
        
    conn.close()
    return cookies

if __name__ == "__main__":
    profile = r"c:\Users\Darwnlin\Desktop\ALL\02_Tool\Brute_and_Bullets\openbulletcookieupdate\OpenBullet-Release\AgentBrowser\debug_browser_profile"
    cookies = extract_cookies_from_profile(profile)
    print(f"Total extracted: {len(cookies)}")
    for c in cookies:
        print(f"[{c['domain']}] {c['name']} = {ascii(c['value'][:40])}... (httpOnly={c['httpOnly']}, secure={c['secure']}, sameSite={c['sameSite']})")
