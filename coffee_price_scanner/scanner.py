"""Precise Amazon scanner for the coffee price tracker (runs as a Home Assistant add-on, or anywhere with Python).

Reads every field from the real page structure (BeautifulSoup + lxml, CSS selectors) - no text slicing -
and hands the raw fields to the Cloudflare Worker (/api/ingest), which computes the delivered-to-Israel
price, history, alerts (Telegram) and the dashboard. The pricing logic lives in one place (the Worker),
so both engines ("github" precise / "cloudflare" light) produce comparable numbers.

Env: WORKER_URL, UPLOAD_KEY (GitHub secrets), FORCE=true to scan regardless of schedule.
CLI: --force  --dry (print parsed fields, don't send)  --loop (run forever: check every 2 minutes)
"""
import json
import os
import random
import re
import sys
from pathlib import Path
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

WORKER = os.environ["WORKER_URL"].rstrip("/")
AUTH = {"Authorization": "Bearer " + os.environ["UPLOAD_KEY"]}
FORCE = "--force" in sys.argv or os.environ.get("FORCE", "").lower() == "true"
DRY = "--dry" in sys.argv
TRANSPORT = os.environ.get("TRANSPORT", "requests")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
REQUEST_DELAY = max(5, float(os.environ.get("REQUEST_DELAY", "10")))
BLOCK_COOLDOWN = 3600
VERSION = "1.1.1"

DOMAIN = {"it": "it", "fr": "fr", "es": "es", "de": "de", "uk": "co.uk", "us": "com"}
LANG = {"it": "it-IT,it;q=0.9", "fr": "fr-FR,fr;q=0.9", "es": "es-ES,es;q=0.9", "de": "de-DE,de;q=0.9",
        "uk": "en-GB,en;q=0.9", "us": "en-US,en;q=0.9"}
CUR = {"it": "EUR", "fr": "EUR", "es": "EUR", "de": "EUR", "uk": "GBP", "us": "USD"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0 Safari/537.36")
KEEP_COOKIES = re.compile(r"^(session-id|session-id-time|session-token|ubid-acb\w*|ubid-main|i18n-prefs|lc-acb\w*|lc-main)$")
DETAIL_LABELS = ["Numéro du modèle", "Référence de pièce", "Numero modello", "Numero del modello", "Numero parte",
                 "Modellnummer", "Teilenummer", "Número de modelo", "Número de pieza", "Item model number",
                 "Model Number", "Model number", "Part Number", "Part number"]
IL = ZoneInfo("Asia/Jerusalem")

import socket
import urllib3.util.connection as _uc
_DEFAULT_FAMILY = _uc.allowed_gai_family


def set_ipv4_only(on):
    """Force IPv4 for all requests (some home networks' IPv6 is geolocated/treated differently by Amazon)."""
    _uc.allowed_gai_family = (lambda: socket.AF_INET) if on else _DEFAULT_FAMILY


IPV4_ONLY = os.environ.get("IPV4_ONLY", "false").lower() == "true"
set_ipv4_only(IPV4_ONLY)


def clean(s):
    return re.sub(r"\s+", " ", (s or "").replace(" ", " ").replace("‎", "").replace("‏", "")).strip()


# ------------------------------------------------------------------ cookies
def jar_dict(jar):
    out = {}
    for part in (jar or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def jar_str(d):
    return "; ".join(f"{k}={v}" for k, v in d.items())


def request_cookies(jar, store):
    d = jar_dict(jar)
    d["i18n-prefs"] = CUR[store]
    locale = LANG[store].split(",", 1)[0].replace("-", "_")
    d["lc-main" if store == "us" else "lc-acb" + store] = locale
    return jar_str(d)


# ------------------------------------------------------------------ precise parser
def parse(html):
    soup = BeautifulSoup(html, "lxml")
    page_title = clean(soup.title.get_text()) if soup.title else ""
    captcha = (soup.select_one('form[action*="validateCaptcha"], #captchacharacters') is not None
               or bool(re.search(r"robot check|captcha", page_title, re.I)))
    img = soup.select_one("#landingImage")
    image = (img.get("data-old-hires") or img.get("src") or "") if img else ""
    # item-details model numbers (tables and bullet lists)
    details = []
    for th in soup.select("table th"):
        if any(lbl.lower() in clean(th.get_text()).lower() for lbl in DETAIL_LABELS):
            td = th.find_next_sibling("td")
            v = clean(td.get_text()) if td else ""
            if v and re.search(r"[A-Za-z]", v) and re.search(r"\d", v) and len(v) <= 40 and v not in details:
                details.append(v)
    for span in soup.select("#detailBullets_feature_div li span.a-text-bold"):
        if any(lbl.lower() in clean(span.get_text()).lower() for lbl in DETAIL_LABELS):
            nxt = span.find_next_sibling("span")
            v = clean(nxt.get_text()) if nxt else ""
            if v and re.search(r"\d", v) and len(v) <= 40 and v not in details:
                details.append(v)
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    def txt(css, limit):
        el = soup.select_one(css)
        return clean(el.get_text(" ")) [:limit] if el else ""

    return {
        "captcha": captcha,
        "title": txt("#productTitle", 300),
        "pageTitle": page_title[:500],
        "to": txt("#glow-ingress-line2", 60),
        "price": txt("#corePrice_feature_div .a-offscreen", 40),
        "price2": txt("#corePriceDisplay_desktop_feature_div .a-offscreen", 40),
        "delivery": txt("#mir-layout-DELIVERY_BLOCK", 300),
        "delivery2": txt("#deliveryBlockMessage", 300),
        "avail": txt("#availability", 200),
        "seller": txt("#merchantInfoFeature_feature_div", 200),
        "returns": txt("#returnsInfoFeature_feature_div", 300),
        "used": txt("#usedBuySection", 200),
        "buybox": txt("#buybox", 800),
        "global": txt("#amazonGlobal_feature_div", 700),
        "image": image,
        "details": details,
    }


def fetch(session, store, asin, jar):
    url = f"https://www.amazon.{DOMAIN[store]}/dp/{asin}"
    r = session.get(url, headers={"User-Agent": UA, "Accept-Language": LANG[store], "Accept": "text/html",
                                  "Cookie": request_cookies(jar, store)}, timeout=40)
    raw = parse(r.text)
    if raw.get("captcha"):
        return raw, jar  # A challenge must never replace the known delivery session.
    if r.status_code != 200:
        return {"status": f"http {r.status_code}"}, jar
    d = jar_dict(jar)
    for c in r.cookies:
        if KEEP_COOKIES.fullmatch(c.name):
            d[c.name] = c.value
    return raw, jar_str(d)


def is_blocked(raw):
    return bool(raw.get("captcha")) or raw.get("status") in ("http 403", "http 429", "http 503")


def load_cooldowns():
    try:
        return json.loads((DATA_DIR / "cooldowns.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_cooldowns(cooldowns):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = DATA_DIR / "cooldowns.tmp"
    temp.write_text(json.dumps(cooldowns))
    temp.replace(DATA_DIR / "cooldowns.json")


def scan_store(state, store, diagnostic=False):
    jar = state["cookies"].get(store, "")
    items = []
    blocked = False
    cooldowns = load_cooldowns()
    cooling = cooldowns.get(store, 0) > time.time()
    browser = None
    session = requests.Session()
    fetched = False
    try:
        for p in state["products"]:
            if store in p.get("skip", []):
                continue
            asins = p["asinsByStore"].get(store)
            if not asins:
                if not diagnostic:
                    items.append({"product": p["key"], "asin": "", "raw": {
                        "status": "not matched" if asins is None else "not listed"}})
                continue
            for asin in asins:
                if blocked or cooling:
                    raw = {"status": "blocked (store cooldown)"}
                else:
                    if fetched:
                        time.sleep(REQUEST_DELAY + random.uniform(0, 3))
                    fetched = True
                    try:
                        if TRANSPORT == "browser":
                            if browser is None:
                                from browser_client import BrowserClient
                                browser = BrowserClient(store, DOMAIN[store], request_cookies(jar, store),
                                                        KEEP_COOKIES, DATA_DIR)
                            raw, candidate_jar = browser.fetch(asin, parse, jar)
                        elif TRANSPORT == "requests":
                            raw, candidate_jar = fetch(session, store, asin, jar)
                        else:
                            raise ValueError("unknown transport")
                        # Accept updated delivery sessions only on real Israel product pages.
                        if (not is_blocked(raw) and raw.get("title")
                                and re.search(r"israel|israël|israele|ישראל", raw.get("to", ""), re.I)):
                            jar = candidate_jar
                    except Exception as e:
                        # Do not log exception text: drivers may include HTML or sensitive URLs.
                        raw = {"status": "error: " + type(e).__name__}
                    if is_blocked(raw):
                        blocked = True
                        cooldowns[store] = time.time() + BLOCK_COOLDOWN
                        save_cooldowns(cooldowns)
                        print(store, "blocked; pausing this store for 60 minutes", flush=True)
                    elif raw.get("status", "").startswith("error:"):
                        # A broken driver/network should not be restarted once per product.
                        blocked = True
                items.append({"product": p["key"], "asin": asin, "raw": raw})
                print(store, asin, {k: raw.get(k) for k in
                      ("captcha", "to", "price", "price2", "delivery", "status")}, flush=True)
                if diagnostic:
                    return {"jar": jar, "items": items}
    finally:
        session.close()
        if browser is not None:
            browser.close()
    return {"jar": jar, "items": items}


# ------------------------------------------------------------------ schedule
def due_slot(state):
    now = datetime.now(IL)
    settings = state["settings"]
    for back in range(0, 3):
        day = (now - timedelta(days=back)).date()
        prime = settings.get("primeAuto") and day.isoformat() in state.get("primeDays", [])
        hours = state["schedules"]["1h" if prime else settings.get("mode", "3x")]
        cands = [h for h in hours if back > 0 or h <= now.hour]
        if cands:
            return f"{day.isoformat()} {max(cands):02d}"
    return None


def run_debug(state):
    """One product per store, using the configured scanner (no duplicate retry pass)."""
    import platform
    info = {"version": VERSION, "transport": TRANSPORT, "platform": platform.platform(),
            "python": platform.python_version(), "ipv4_only_setting": IPV4_ONLY,
            "request_delay": REQUEST_DELAY}
    results = {}
    for store in state["stores"]:
        data = scan_store(state, store, diagnostic=True)
        results[store] = [{"asin": i["asin"], **i["raw"]} for i in data["items"]]
        time.sleep(REQUEST_DELAY)
    if DRY:
        print(json.dumps({"info": info, "results": results}, ensure_ascii=True))
        return
    r = requests.post(WORKER + "/api/ingest", headers=AUTH,
                      json={"debug": True, "info": info, "results": results}, timeout=60)
    r.raise_for_status()
    print("debug report sent:", r.status_code, flush=True)


def main():
    response = requests.get(WORKER + "/api/state", headers=AUTH, timeout=60)
    response.raise_for_status()
    state = response.json()
    if str(state.get("scanRequest") or "").startswith("debug"):
        run_debug(state)
        return 0
    engine = state["settings"].get("engine", "cloudflare")
    requested = bool(state.get("scanRequest"))          # "scan now" pressed on the dashboard
    force = FORCE or requested
    if engine == "cloudflare" and not force:
        return 0                                        # light engine selected - nothing to do
    slot = due_slot(state)
    if not force and slot == state.get("lastScanSlot"):
        return 0                                        # this slot was already scanned
    print(datetime.now(IL).strftime("%Y-%m-%d %H:%M"), "scanning slot", slot, "(requested)" if requested else "(force)" if FORCE else "", flush=True)
    payload = {"slot": slot, "engine": "home", "stores": {}}
    for store in state["stores"]:
        payload["stores"][store] = scan_store(state, store)
        print(store, "done:", len(payload["stores"][store]["items"]), "items", flush=True)
        time.sleep(REQUEST_DELAY)
    if DRY:
        print("dry run - not sending")
        return 0
    r = requests.post(WORKER + "/api/ingest", headers=AUTH, json=payload, timeout=120)
    print("ingest:", r.status_code)
    if r.ok:
        for row in r.json().get("rows", []):
            print("  ", row)
    return 0 if r.ok else 1


if __name__ == "__main__":
    if "--loop" in sys.argv:
        print(f"coffee scanner {VERSION} started (transport={TRANSPORT}; checks every 2 minutes)", flush=True)
        while True:
            try:
                main()
            except Exception as e:  # keep the add-on alive on network errors
                print("error:", type(e).__name__, flush=True)
            time.sleep(120)
    sys.exit(main())
