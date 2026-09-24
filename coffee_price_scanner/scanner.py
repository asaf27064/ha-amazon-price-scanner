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
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

WORKER = os.environ["WORKER_URL"].rstrip("/")
AUTH = {"Authorization": "Bearer " + os.environ["UPLOAD_KEY"]}
FORCE = "--force" in sys.argv or os.environ.get("FORCE", "").lower() == "true"
DRY = "--dry" in sys.argv

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
    if store == "us":
        d["lc-main"] = "en_US"
    return jar_str(d)


# ------------------------------------------------------------------ precise parser
def parse(html):
    soup = BeautifulSoup(html, "lxml")
    captcha = soup.select_one('form[action*="validateCaptcha"]') is not None
    page_title = clean(soup.title.get_text()) if soup.title else ""
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
    d = jar_dict(jar)
    for c in r.cookies:
        if KEEP_COOKIES.match(c.name):
            d[c.name] = c.value
    jar = jar_str(d)
    if r.status_code != 200:
        return {"status": f"http {r.status_code}"}, jar
    return parse(r.text), jar


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
    """Diagnostics: what does Amazon return to THIS machine? Sent to the Worker without touching any data."""
    info = {"ipv4_only_setting": IPV4_ONLY}
    for name, url in [("ipinfo", "https://ipinfo.io/json"), ("ipv6", "https://api6.ipify.org?format=json"),
                      ("ipv4", "https://api4.ipify.org?format=json")]:
        try:
            info[name] = requests.get(url, timeout=12).json()
        except Exception as e:
            info[name] = "error: " + str(e)[:80]
    try:
        info["amazon_it_resolves_to"] = sorted({a[4][0] for a in socket.getaddrinfo("www.amazon.it", 443)})[:6]
    except Exception as e:
        info["amazon_it_resolves_to"] = str(e)[:80]
    session = requests.Session()
    out = {}
    tests = []
    for store in state["stores"]:
        for p in state["products"][:2]:
            lst = p["asinsByStore"].get(store) or []
            if store not in p.get("skip", []) and lst:
                tests.append((store, lst[0]))
    for mode in (["default", "ipv4"] if not IPV4_ONLY else ["ipv4"]):
        set_ipv4_only(mode == "ipv4")
        rows = []
        for store, asin in (tests if mode == "ipv4" or IPV4_ONLY else tests[:4]):
            url = f"https://www.amazon.{DOMAIN[store]}/dp/{asin}"
            try:
                r = session.get(url, headers={"User-Agent": UA, "Accept-Language": LANG[store], "Accept": "text/html",
                                              "Cookie": request_cookies(state["cookies"].get(store, ""), store)}, timeout=40)
                raw = parse(r.text) if r.status_code == 200 else {}
                rows.append({"store": store, "asin": asin, "http": r.status_code, "final_url": r.url[:120], "bytes": len(r.content),
                             **{k: (raw.get(k) or "")[:160] if isinstance(raw.get(k), str) else raw.get(k)
                                for k in ("captcha", "to", "price", "delivery", "delivery2", "avail", "global", "pageTitle")}})
            except Exception as e:
                rows.append({"store": store, "asin": asin, "error": str(e)[:120]})
            time.sleep(random.uniform(1.5, 2.5))
        out[mode] = rows
    set_ipv4_only(IPV4_ONLY)
    r = requests.post(WORKER + "/api/ingest", headers=AUTH, json={"debug": True, "info": info, "results": out}, timeout=60)
    print("debug report sent:", r.status_code, flush=True)


def main():
    state = requests.get(WORKER + "/api/state", headers=AUTH, timeout=60).json()
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
    session = requests.Session()
    payload = {"slot": slot if not requested or slot != state.get("lastScanSlot") else slot, "engine": "home", "stores": {}}
    for store in state["stores"]:
        jar = state["cookies"].get(store, "")
        items = []
        for p in state["products"]:
            if store in p.get("skip", []):
                continue
            lst = p["asinsByStore"].get(store)
            if lst is None or not lst:
                items.append({"product": p["key"], "asin": "", "raw": {"status": "not matched" if lst is None else "not listed"}})
                continue
            for asin in lst:
                try:
                    raw, jar = fetch(session, store, asin, jar)
                    if raw.get("captcha"):  # one polite retry
                        time.sleep(8)
                        raw, jar = fetch(session, store, asin, jar)
                except Exception as e:  # network hiccup
                    raw = {"status": "error: " + str(e)[:60]}
                items.append({"product": p["key"], "asin": asin, "raw": raw})
                if DRY:
                    print(store, asin, {k: raw.get(k) for k in ("to", "price", "price2", "delivery", "avail", "status")})
                time.sleep(random.uniform(1.2, 2.5))
        payload["stores"][store] = {"jar": jar, "items": items}
        print(store, "done:", len(items), "items")
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
        print("coffee scanner started (checks every 2 minutes)", flush=True)
        while True:
            try:
                main()
            except Exception as e:  # keep the add-on alive on network errors
                print("error:", e, flush=True)
            time.sleep(120)
    sys.exit(main())
