#!/usr/bin/env python3
"""
dflight_sync.py
Scarica le zone di volo da D-Flight e aggiorna il repository GitHub.
Usa Playwright (headless Chromium) per gestire autenticazione SPA + CSRF.

Uso locale:
    pip install playwright && playwright install chromium
    DFLIGHT_USER=tuo@email.it DFLIGHT_PASS=tuapassword python dflight_sync.py

In GitHub Actions le credenziali vengono iniettate come secrets (vedi sync-zones.yml).
"""

import os
import sys
import json
import gzip
import time
from datetime import date, datetime, timezone

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
BASE_URL     = "https://www.d-flight.it"
TOKEN_URL    = f"{BASE_URL}/auth-iam/token"
DOWNLOAD_URL = f"{BASE_URL}/geo-awareness/api/ed-269/geo-zones/download"

# URL della SPA Angular (non la homepage WordPress)
APP_URLS = [
    f"{BASE_URL}/private/dashboard",
    f"{BASE_URL}/private",
    f"{BASE_URL}/dflight",
]

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


def try_get_xsrf_via_app_navigation(page, context):
    """Naviga all'app Angular e aspetta il cookie XSRF-TOKEN."""
    for app_url in APP_URLS:
        try:
            print(f"   → Provo {app_url}")
            page.goto(app_url, wait_until="domcontentloaded", timeout=25_000)
            # Aspetta l'inizializzazione Angular (max 8s)
            for _ in range(8):
                time.sleep(1)
                cookies = context.cookies()
                xsrf = next((c["value"] for c in cookies if c["name"] == "XSRF-TOKEN"), None)
                if xsrf:
                    print(f"   ✅ XSRF-TOKEN trovato dopo navigazione a {app_url}")
                    return xsrf
        except PlaywrightTimeout:
            print(f"   ⚠️  Timeout su {app_url}")
        except Exception as e:
            print(f"   ⚠️  Errore su {app_url}: {e}")
    return None


def try_login_via_form(page, context):
    """
    Compila il form di login Angular e intercetta la risposta del token.
    Ritorna l'access_token se trovato, None altrimenti.
    """
    captured = {}

    def on_response(response):
        if ("token" in response.url.lower() or "iam" in response.url.lower()) and response.status == 200:
            try:
                data = response.json()
                if data.get("access_token"):
                    captured["access_token"] = data["access_token"]
                    print(f"   ✅ Token intercettato da {response.url}")
            except Exception:
                pass

    page.on("response", on_response)

    # Prova prima ad andare all'app, poi cerca il form
    for app_url in APP_URLS:
        try:
            page.goto(app_url, wait_until="domcontentloaded", timeout=25_000)
            page.wait_for_timeout(3000)

            # Selettori comuni per campo email/username in Angular
            email_selectors = [
                "input[type='email']",
                "input[formcontrolname='username']",
                "input[formcontrolname='email']",
                "input[name='username']",
                "input[name='email']",
                "input[placeholder*='email' i]",
                "input[placeholder*='utente' i]",
                "input[placeholder*='user' i]",
            ]
            email_el = None
            for sel in email_selectors:
                try:
                    page.wait_for_selector(sel, timeout=2000)
                    email_el = sel
                    break
                except Exception:
                    pass

            if not email_el:
                print(f"   ⚠️  Form non trovato su {app_url}")
                continue

            print(f"   ✅ Form trovato su {app_url} (selettore: {email_el})")

            # Compila il form
            page.fill(email_el, DFLIGHT_USER)
            for sel in ["input[type='password']", "input[formcontrolname='password']", "input[name='password']"]:
                try:
                    page.fill(sel, DFLIGHT_PASS)
                    break
                except Exception:
                    pass

            # Click sul pulsante di login
            for sel in [
                "button[type='submit']",
                "input[type='submit']",
                "button:has-text('Accedi')",
                "button:has-text('Login')",
                "button:has-text('Entra')",
                "[class*='login' i] button",
                "form button",
            ]:
                try:
                    page.click(sel, timeout=2000)
                    break
                except Exception:
                    pass

            # Aspetta la risposta del token (max 10s)
            for _ in range(10):
                time.sleep(1)
                if captured.get("access_token"):
                    return captured["access_token"]

        except PlaywrightTimeout:
            print(f"   ⚠️  Timeout su {app_url}")
        except Exception as e:
            print(f"   ⚠️  Errore su {app_url}: {e}")

    page.remove_listener("response", on_response)
    return captured.get("access_token")


# ---------------------------------------------------------------------------
# Main
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
    )
    page = context.new_page()

    # ------------------------------------------------------------------
    # Strategia 1: naviga alla SPA Angular → ottieni XSRF-TOKEN → POST diretto
    # ------------------------------------------------------------------
    print("🔑 Strategia 1: navigazione SPA → XSRF-TOKEN...")
    xsrf_token = try_get_xsrf_via_app_navigation(page, context)

    if xsrf_token:
        print("🔐 Login via page.request con XSRF-TOKEN...")
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
            headers={
                "Content-Type":    "application/x-www-form-urlencoded",
                "Accept":          "application/json, text/plain, */*",
                "X-Requested-With":"XMLHttpRequest",
                "X-XSRF-TOKEN":    xsrf_token,
                "X-CSRF-TOKEN":    xsrf_token,
                "Origin":          BASE_URL,
                "Referer":         f"{BASE_URL}/",
            },
            timeout=30_000,
        )
        if login_resp.status == 200:
            token_data = login_resp.json()
            access_token = token_data.get("access_token")
            if access_token:
                print("✅ Login riuscito (strategia 1).")
        else:
            print(f"⚠️  Strategia 1 fallita: {login_resp.status} — {login_resp.text()[:300]}")

    # ------------------------------------------------------------------
    # Strategia 2: compila il form Angular e intercetta il token
    # ------------------------------------------------------------------
    if not access_token:
        print("🔑 Strategia 2: login via form Angular + intercettazione risposta...")
        access_token = try_login_via_form(page, context)
        if access_token:
            print("✅ Login riuscito (strategia 2).")
        else:
            print("❌ Entrambe le strategie di login fallite.")
            # Salva screenshot per debug
            try:
                page.screenshot(path="debug_screenshot.png")
                print("📸 Screenshot salvato in debug_screenshot.png")
            except Exception:
                pass
            browser.close()
            sys.exit(1)

    # ------------------------------------------------------------------
    # Download zone
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
        print(f"❌ Download fallito: {zones_resp.status} — {zones_resp.text()[:300]}")
        browser.close()
        sys.exit(1)

    zones_content = zones_resp.body()
    print(f"✅ Download completato ({len(zones_content) / 1024:.0f} KB).")

    browser.close()

# ---------------------------------------------------------------------------
# Decomprimi gzip → JSON
# ---------------------------------------------------------------------------
print("📦 Decompressione / lettura JSON...")
json_str   = None
json_bytes = None

try:
    json_bytes = gzip.decompress(zones_content)
    json_str   = json_bytes.decode("utf-8")
    json.loads(json_str)
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
# Salva file zone
# ---------------------------------------------------------------------------
os.makedirs(ZONES_DIR, exist_ok=True)

with open(ZONES_FILE, "w", encoding="utf-8") as f:
    f.write(json_str)
print(f"✅ Zone salvate in {ZONES_FILE}")

# ---------------------------------------------------------------------------
# Aggiorna metadata.json
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
