#!/usr/bin/env python3
"""
dflight_sync.py
Scarica le zone di volo da D-Flight e aggiorna il repository GitHub.
Usa Playwright (headless Chromium) per gestire CSRF e autenticazione SPA.

Uso locale:
    pip install playwright && playwright install chromium
    DFLIGHT_USER=tuo@email.it DFLIGHT_PASS=tuapassword python dflight_sync.py

In GitHub Actions le credenziali vengono iniettate come secrets (vedi sync-zones.yml).
"""

import os
import sys
import json
import gzip
from datetime import date, datetime, timezone

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
BASE_URL     = "https://www.d-flight.it"
TOKEN_URL    = f"{BASE_URL}/auth-iam/token"
DOWNLOAD_URL = f"{BASE_URL}/geo-awareness/api/ed-269/geo-zones/download"

ZONES_DIR     = "zones"
ZONES_FILE    = f"{ZONES_DIR}/italy_zones.json"
METADATA_FILE = f"{ZONES_DIR}/metadata.json"

# ---------------------------------------------------------------------------
# Leggi credenziali da environment
# ---------------------------------------------------------------------------
DFLIGHT_USER = os.environ.get("DFLIGHT_USER")
DFLIGHT_PASS = os.environ.get("DFLIGHT_PASS")

if not DFLIGHT_USER or not DFLIGHT_PASS:
    print("❌ DFLIGHT_USER e DFLIGHT_PASS devono essere impostati come variabili d'ambiente.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Login con Playwright (headless Chromium)
# ---------------------------------------------------------------------------
access_token = None
zones_content = None

with sync_playwright() as p:
    print("🌐 Avvio browser headless...")
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        extra_http_headers={
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
        },
    )
    page = context.new_page()

    # ------------------------------------------------------------------
    # 1. Carica homepage per ottenere cookie CSRF (Angular imposta XSRF-TOKEN)
    # ------------------------------------------------------------------
    print("🌐 Caricamento homepage D-Flight...")
    try:
        page.goto(BASE_URL, wait_until="networkidle", timeout=30_000)
    except PlaywrightTimeout:
        print("⚠️  Timeout networkidle, continuo comunque...")

    # Leggi XSRF-TOKEN dai cookie del browser
    cookies = context.cookies()
    xsrf_token = next(
        (c["value"] for c in cookies if c["name"] in ("XSRF-TOKEN", "csrf", "CSRF-TOKEN")),
        None,
    )
    print(f"   → cookies: {[c['name'] for c in cookies]}, xsrf: {bool(xsrf_token)}")

    # ------------------------------------------------------------------
    # 2. POST /auth-iam/token usando page.request (stesso cookie jar del browser)
    # ------------------------------------------------------------------
    print("🔐 Login su D-Flight...")

    login_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
    }
    if xsrf_token:
        login_headers["X-XSRF-TOKEN"] = xsrf_token
        login_headers["X-CSRF-TOKEN"]  = xsrf_token

    login_resp = page.request.post(
        TOKEN_URL,
        form={
            "scope": (
                "openid email profile user-data personal-data "
                "pilot-license dflight-identification"
            ),
            "grant_type": "password",
            "client_id":  "web-app",
            "username":   DFLIGHT_USER,
            "password":   DFLIGHT_PASS,
        },
        headers=login_headers,
        timeout=30_000,
    )

    if login_resp.status != 200:
        body = login_resp.text()
        print(f"❌ Login fallito: {login_resp.status} — {body[:400]}")
        browser.close()
        sys.exit(1)

    token_data = login_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        print(f"❌ access_token non trovato nella risposta: {json.dumps(token_data)[:300]}")
        browser.close()
        sys.exit(1)

    print("✅ Login riuscito.")

    # ------------------------------------------------------------------
    # 3. Download zone
    # ------------------------------------------------------------------
    print("📥 Download zone di volo...")

    zones_resp = page.request.get(
        DOWNLOAD_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept":        "application/octet-stream, application/json, */*",
        },
        timeout=120_000,
    )

    if zones_resp.status != 200:
        body = zones_resp.text()
        print(f"❌ Download fallito: {zones_resp.status} — {body[:300]}")
        browser.close()
        sys.exit(1)

    zones_content = zones_resp.body()
    print(f"✅ Download completato ({len(zones_content) / 1024:.0f} KB).")

    browser.close()

# ---------------------------------------------------------------------------
# 4. Decomprimi gzip → JSON
# ---------------------------------------------------------------------------
print("📦 Decompressione / lettura JSON...")

json_str   = None
json_bytes = None

try:
    json_bytes = gzip.decompress(zones_content)
    json_str   = json_bytes.decode("utf-8")
    json.loads(json_str)   # valida
    print(f"✅ JSON gzip valido ({len(json_bytes) / 1024:.0f} KB decompressi).")
except Exception as e:
    print(f"⚠️  Non è gzip ({e}), provo come JSON diretto...")
    try:
        json_str   = zones_content.decode("utf-8")
        json_bytes = zones_content
        json.loads(json_str)
        print(f"✅ JSON diretto valido ({len(json_bytes) / 1024:.0f} KB).")
    except Exception as e2:
        print(f"❌ Impossibile leggere il file zone: {e2}")
        sys.exit(1)

# ---------------------------------------------------------------------------
# 5. Salva file zone
# ---------------------------------------------------------------------------
os.makedirs(ZONES_DIR, exist_ok=True)

with open(ZONES_FILE, "w", encoding="utf-8") as f:
    f.write(json_str)

print(f"✅ Zone salvate in {ZONES_FILE}")

# ---------------------------------------------------------------------------
# 6. Aggiorna metadata.json
# ---------------------------------------------------------------------------
version = 1
if os.path.exists(METADATA_FILE):
    try:
        with open(METADATA_FILE, "r") as f:
            old_meta = json.load(f)
        version = old_meta.get("version", 0) + 1
    except Exception:
        version = 1

metadata = {
    "date":       date.today().isoformat(),
    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "source":     "d-flight.it",
    "version":    version,
    "size_bytes": len(json_bytes),
}

with open(METADATA_FILE, "w", encoding="utf-8") as f:
    json.dump(metadata, f, indent=2)

print(f"✅ Metadata aggiornati: versione {version}, data {metadata['date']}")
print("🎉 Sync completato con successo.")
