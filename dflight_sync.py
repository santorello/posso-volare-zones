#!/usr/bin/env python3
"""
dflight_sync.py
Scarica le zone di volo da D-Flight e aggiorna il repository GitHub.

Uso locale:
    DFLIGHT_USER=tuo@email.it DFLIGHT_PASS=tuapassword python dflight_sync.py

In GitHub Actions le credenziali vengono iniettate come secrets (vedi sync-zones.yml).
"""

import os
import sys
import json
import gzip
import requests
from datetime import date, datetime, timezone

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
BASE_URL    = "https://www.d-flight.it"
LOGIN_PAGE  = f"{BASE_URL}/"
TOKEN_URL   = f"{BASE_URL}/auth-iam/token"
DOWNLOAD_URL = f"{BASE_URL}/geo-awareness/api/ed-269/geo-zones/download"

ZONES_DIR     = "zones"
ZONES_FILE    = f"{ZONES_DIR}/italy_zones.json"
METADATA_FILE = f"{ZONES_DIR}/metadata.json"

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Origin":  BASE_URL,
    "Referer": f"{BASE_URL}/",
}

# ---------------------------------------------------------------------------
# 1. Leggi credenziali da environment
# ---------------------------------------------------------------------------
DFLIGHT_USER = os.environ.get("DFLIGHT_USER")
DFLIGHT_PASS = os.environ.get("DFLIGHT_PASS")

if not DFLIGHT_USER or not DFLIGHT_PASS:
    print("❌ DFLIGHT_USER e DFLIGHT_PASS devono essere impostati come variabili d'ambiente.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# 2. Sessione — pre-flight GET per ottenere cookie/token CSRF
# ---------------------------------------------------------------------------
session = requests.Session()
session.headers.update(HEADERS_BASE)

print("🌐 Pre-flight GET per cookie CSRF...")
csrf_token = None
try:
    pre = session.get(LOGIN_PAGE, timeout=15, allow_redirects=True)
    # Cerca XSRF-TOKEN nei cookie (pattern Spring Security / Angular)
    csrf_token = session.cookies.get("XSRF-TOKEN") or session.cookies.get("csrf")
    # Cerca anche negli header della risposta
    if not csrf_token:
        csrf_token = pre.headers.get("X-CSRF-TOKEN") or pre.headers.get("X-XSRF-TOKEN")
    print(f"   → {pre.status_code}, cookies: {list(session.cookies.keys())}, csrf: {bool(csrf_token)}")
except Exception as e:
    print(f"⚠️  Pre-flight fallita (continuo comunque): {e}")

# ---------------------------------------------------------------------------
# 3. Login → ottieni access token
# ---------------------------------------------------------------------------
print("🔐 Login su D-Flight...")

login_headers = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}
if csrf_token:
    login_headers["X-XSRF-TOKEN"] = csrf_token
    login_headers["X-CSRF-TOKEN"] = csrf_token

login_resp = session.post(
    TOKEN_URL,
    data={
        "scope": "openid email profile user-data personal-data pilot-license dflight-identification",
        "grant_type": "password",
        "client_id": "web-app",
        "username": DFLIGHT_USER,
        "password": DFLIGHT_PASS,
    },
    headers=login_headers,
    timeout=30,
)

if login_resp.status_code != 200:
    print(f"❌ Login fallito: {login_resp.status_code} — {login_resp.text[:300]}")
    sys.exit(1)

access_token = login_resp.json().get("access_token")
if not access_token:
    print("❌ access_token non trovato nella risposta di login.")
    sys.exit(1)

print("✅ Login riuscito.")

# ---------------------------------------------------------------------------
# 4. Download zone (file .json.gz)
# ---------------------------------------------------------------------------
print("📥 Download zone di volo...")

zones_resp = session.get(
    DOWNLOAD_URL,
    headers={
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/plain, */*",
    },
    timeout=120,
)

if zones_resp.status_code != 200:
    print(f"❌ Download fallito: {zones_resp.status_code} — {zones_resp.text[:200]}")
    sys.exit(1)

print(f"✅ Download completato ({len(zones_resp.content) / 1024:.0f} KB compressi).")

# ---------------------------------------------------------------------------
# 5. Decomprimi gzip → JSON
# ---------------------------------------------------------------------------
print("📦 Decompressione gzip...")

try:
    json_bytes = gzip.decompress(zones_resp.content)
    json_str   = json_bytes.decode("utf-8")
    json.loads(json_str)  # valida
    print(f"✅ JSON valido ({len(json_bytes) / 1024:.0f} KB decompressi).")
except Exception as e:
    # Potrebbe non essere gzip — proviamo a usarlo direttamente
    print(f"⚠️  Gzip fallito ({e}), provo a leggere il body diretto...")
    try:
        json_str   = zones_resp.text
        json_bytes = json_str.encode("utf-8")
        json.loads(json_str)
        print(f"✅ JSON diretto valido ({len(json_bytes) / 1024:.0f} KB).")
    except Exception as e2:
        print(f"❌ Impossibile leggere il file zone: {e2}")
        sys.exit(1)

# ---------------------------------------------------------------------------
# 6. Salva file zone
# ---------------------------------------------------------------------------
os.makedirs(ZONES_DIR, exist_ok=True)

with open(ZONES_FILE, "w", encoding="utf-8") as f:
    f.write(json_str)

print(f"✅ Zone salvate in {ZONES_FILE}")

# ---------------------------------------------------------------------------
# 7. Aggiorna metadata.json
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
    "date":        date.today().isoformat(),
    "updated_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "source":      "d-flight.it",
    "version":     version,
    "size_bytes":  len(json_bytes),
}

with open(METADATA_FILE, "w", encoding="utf-8"