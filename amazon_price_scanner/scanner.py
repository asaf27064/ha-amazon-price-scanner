"""Precise Amazon scanner for the Amazon price tracker (runs as a Home Assistant add-on, or anywhere with Python).

Reads every field from the real page structure (BeautifulSoup + lxml, CSS selectors) - no text slicing -
and hands the raw fields to the Cloudflare Worker (/api/ingest), which computes the delivered-to-Israel
price, history, alerts (Telegram) and the dashboard. The pricing logic lives in one place (the Worker),
so both engines ("home" precise / "cloudflare" light) produce comparable numbers.

Env: WORKER_URL, UPLOAD_KEY (add-on options), FORCE=true to scan regardless of schedule.
CLI: --force  --dry (print parsed fields, don't send)  --loop (run forever: scans every 2 minutes,
     "add product" checks polled every 5 seconds)
"""
import json
import os
import random
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
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
VERSION = "2.0.0"
CHECK_CONCURRENCY = min(6, max(1, int(os.environ.get("CHECK_CONCURRENCY", "3"))))
JOB_POLL = 5
MAX_INGEST_RETRIES = 5          # a failed ingest is re-sent (same payload), the slot is not rescanned

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
    variations = variations_from(html)
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
        "variations": variations,
    }


def variations_from(html):
    """Sibling variations (colour / style) listed on a product page: [{asin, label}]."""
    out = {}
    m = re.search(r'"dimensionValuesDisplayData"\s*:\s*(\{[^{}]*\})', html)
    if m:
        try:
            for asin, v in json.loads(m.group(1)).items():
                if re.fullmatch(r"[A-Z0-9]{10}", asin):
                    out[asin] = " / ".join(v if isinstance(v, list) else [v])
        except ValueError:
            pass
    for pat, a, lbl in ((r'"defaultAsin":"([A-Z0-9]{10})"[^{}]{0,200}?"dimensionValueDisplayText":"([^"]*)"', 1, 2),
                        (r'"dimensionValueDisplayText":"([^"]*)"[^{}]{0,200}?"defaultAsin":"([A-Z0-9]{10})"', 2, 1)):
        for mm in re.finditer(pat, html):
            if mm.group(a) not in out:
                try:
                    out[mm.group(a)] = json.loads('"' + mm.group(lbl) + '"')
                except ValueError:
                    out[mm.group(a)] = mm.group(lbl)
    return [{"asin": k, "label": clean(v)[:60]} for k, v in list(out.items())[:12]]


ACCESSORY = re.compile(r"compatib|kompatib|replacement|ricambi|remplacement|ersatz|repuesto|recambio|spare part|"
                       r"pi[eè]ce de rechange", re.I)
MODEL_TOKEN = re.compile(r"\b[A-Z]{1,6}\d{2,}[A-Z0-9]*(?:[./-][A-Z0-9]{1,4})*\b")


def norm(x):
    return re.sub(r"[^A-Z0-9]", "", (x or "").upper())


def model_core(model):
    """"ECAM472.50.B" -> "ECAM47250": the part that must appear on the matching page."""
    parts = re.split(r"[./-]", (model or "").upper())
    core = norm(parts[0])
    for part in parts[1:]:
        if re.search(r"\d", part):
            core += norm(part)
        else:
            break
    return core


def page_matches(raw, model):
    """Is this product page the product we are looking for? Item-details model numbers first, title second."""
    if not raw or raw.get("status") or raw.get("captcha") or not raw.get("title"):
        return False
    if not model:
        return True
    core = model_core(model)
    if raw.get("details"):
        return any(core in norm(d) for d in raw["details"])
    title = f'{raw.get("title", "")} {raw.get("pageTitle", "")}'
    return core in norm(title) or not MODEL_TOKEN.search(title.upper())


def parse_search(html):
    """Search results page -> [{asin, title}] (accessory listings removed)."""
    soup = BeautifulSoup(html, "lxml")
    hits = []
    for div in soup.select('div[data-component-type="s-search-result"][data-asin]'):
        asin = div.get("data-asin", "")
        h2 = div.select_one("h2")
        title = clean((h2.get("aria-label") or "") + " " + h2.get_text(" ")) if h2 else ""
        if re.fullmatch(r"[A-Z0-9]{10}", asin) and title and not ACCESSORY.search(title):
            hits.append({"asin": asin, "title": title})
    return hits


def fetch(session, store, asin, jar, query=""):
    url = f"https://www.amazon.{DOMAIN[store]}/dp/{asin}{query}"
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


# Amazon's own retail offer per marketplace: dp/ASIN?smid=<id> shows it even when a marketplace seller has the buy box.
AMAZON_SMID = {"it": "A11IL2PNWYJU7H", "fr": "A1X6FK5RDHNB96", "es": "A1AT7YVPFBWXBL", "de": "A3JWKAKR8XB7XF",
               "uk": "A3P5ROKL5A1OLE", "us": "A2XZ7JICGUQ1CX"}
SHIP_L = r"Ships from|Dispatches from|Spedito da|Speditore|Expédié par|Expéditeur|Enviado por|Remitente|Versand durch|Versender"
SOLD_L = (r"Ships from and sold by|Dispatched from and sold by|Venduto e spedito da|Vendu et expédié par|"
          r"Vendido y enviado por|Verkauf und Versand durch|"
          r"Sold by|Venduto da|Venditore|Vendu par|Vendeur|Vendido por|Vendedor|Verkauf durch|Verkäufer")
ALT_KEYS = ("to", "title", "price", "price2", "delivery", "delivery2", "avail", "seller", "returns", "buybox", "global")


def seller_kind(raw):
    """"amazon" (sold by Amazon), "other" (a marketplace seller) or "" (unknown)."""
    text = re.sub(r"\s+", " ", f'{raw.get("seller", "")} | {raw.get("buybox", "")}')
    if re.search(rf"(?:{SHIP_L}|Shipper|Dispatcher)\s*/\s*(?:{SOLD_L}|Seller)\s*:?\s*Amazon", text, re.I):
        return "amazon"
    m = re.search(rf"(?:{SOLD_L})\s*:?\s*(\S+)", text, re.I)
    if m:
        return "amazon" if m.group(1).lower().startswith("amazon") else "other"
    return "amazon" if raw.get("seller", "").strip().lower().startswith("amazon") else ""


def prefer_amazon(get, store, asin, raw):
    """When a marketplace seller has the buy box, read Amazon's own offer too (one extra page).
    Returns (page, blocked): Amazon's offer with the marketplace offer attached as "alt" (or the original
    page), and whether Amazon answered the extra page with a challenge (the caller pauses the store)."""
    if raw.get("status") or raw.get("captcha") or not (raw.get("price") or raw.get("price2")) or seller_kind(raw) != "other":
        return raw, False
    query = f"?smid={AMAZON_SMID[store]}&psc=1"
    try:
        own = get(query)
    except Exception:
        return raw, False
    if is_blocked(own):
        return raw, True
    if own.get("status") or seller_kind(own) != "amazon" or not (own.get("price") or own.get("price2")):
        return raw, False
    own["alt"] = {k: raw.get(k, "") for k in ALT_KEYS}
    own["url"] = f"https://www.amazon.{DOMAIN[store]}/dp/{asin}{query}"
    own.pop("variations", None)
    if raw.get("variations"):
        own["variations"] = raw["variations"]
    return own, False


def is_blocked(raw):
    return bool(raw.get("captcha")) or raw.get("status") in ("http 403", "http 429", "http 503")


def load_cooldowns():
    try:
        return json.loads((DATA_DIR / "cooldowns.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}


_COOLDOWN_LOCK = threading.Lock()


def set_cooldown(store):
    with _COOLDOWN_LOCK:
        cooldowns = load_cooldowns()
        cooldowns[store] = time.time() + BLOCK_COOLDOWN
        save_cooldowns(cooldowns)


def save_cooldowns(cooldowns):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = DATA_DIR / "cooldowns.tmp"
    temp.write_text(json.dumps(cooldowns))
    temp.replace(DATA_DIR / "cooldowns.json")


def close_quietly(*things):
    for thing in things:
        try:
            if thing is not None:
                thing.close()
        except Exception:
            pass


def scan_store(state, store, diagnostic=False):
    jar = state["cookies"].get(store, "")
    items = []
    blocked = False
    aborted = False
    errors_in_row = 0
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
                elif aborted:
                    raw = {"status": "skipped (store aborted)"}
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
                            if seller_kind(raw) == "other":
                                time.sleep(REQUEST_DELAY / 2 + random.uniform(0, 2))

                                def get(query, asin=asin):
                                    if browser is not None:
                                        return browser.fetch(asin, parse, jar, query)[0]
                                    return fetch(session, store, asin, jar, query)[0]
                                raw, smid_blocked = prefer_amazon(get, store, asin, raw)
                                if smid_blocked:
                                    blocked = True
                                    cooldowns[store] = time.time() + BLOCK_COOLDOWN
                                    save_cooldowns(cooldowns)
                                    print(store, "blocked on the Amazon-offer page; pausing this store for 60 minutes",
                                          flush=True)
                    except Exception as e:
                        # Do not log exception text: drivers may include HTML or sensitive URLs.
                        raw = {"status": "error: " + type(e).__name__}
                    if is_blocked(raw):
                        blocked = True
                        cooldowns[store] = time.time() + BLOCK_COOLDOWN
                        save_cooldowns(cooldowns)
                        print(store, "blocked; pausing this store for 60 minutes", flush=True)
                    elif raw.get("status", "").startswith("error:"):
                        # One missing page (removed ASIN) or one slow page is a per-product result; only a
                        # driver/network that keeps failing stops the store (not restarted once per product).
                        errors_in_row += 1
                        if errors_in_row >= 2:
                            aborted = True
                            print(store, "two errors in a row; skipping the rest of this store", flush=True)
                    else:
                        errors_in_row = 0
                items.append({"product": p["key"], "asin": asin, "raw": raw})
                print(store, asin, {k: raw.get(k) for k in
                      ("captcha", "to", "price", "price2", "delivery", "status")}, flush=True)
                if diagnostic:
                    return {"jar": jar, "items": items}
    finally:
        close_quietly(session, browser)
    return {"jar": jar, "items": items}


# ------------------------------------------------------------------ "add product" check (fast, parallel)
class Blocked(Exception):
    """Amazon answered with a challenge / rate limit: pause this store."""


class StoreClient:
    """One marketplace session for a product check, using the configured transport. Checks may run while a
    scheduled scan is using the store's normal Chromium profile, so they use their own profiles (DATA_DIR/check)
    and keep the same pacing between pages as the scan (half the request delay)."""

    def __init__(self, store, jar):
        self.store, self.jar = store, jar
        self.session = requests.Session()
        self.browser = None
        self.pages = 0

    def _pace(self):
        if self.pages:
            time.sleep(REQUEST_DELAY / 2 + random.uniform(0, 1.5))
        self.pages += 1

    def _browser(self):
        if self.browser is None:
            from browser_client import BrowserClient
            self.browser = BrowserClient(self.store, DOMAIN[self.store], request_cookies(self.jar, self.store),
                                         KEEP_COOKIES, DATA_DIR / "check")
        return self.browser

    def product(self, asin, query=""):
        self._pace()
        if TRANSPORT == "browser":
            raw, _ = self._browser().fetch(asin, parse, self.jar, query)
        else:
            raw, _ = fetch(self.session, self.store, asin, self.jar, query)
        return raw

    def search(self, model):
        self._pace()
        url = f"https://www.amazon.{DOMAIN[self.store]}/s?k={requests.utils.quote(model)}"
        if TRANSPORT == "browser":
            html = self._browser().get_html(url)
        else:
            r = self.session.get(url, headers={"User-Agent": UA, "Accept-Language": LANG[self.store], "Accept": "text/html",
                                               "Cookie": request_cookies(self.jar, self.store)}, timeout=40)
            if r.status_code in (403, 429, 503):
                raise Blocked()
            html = r.text if r.status_code == 200 else ""
        if html and parse(html).get("captcha"):
            raise Blocked()
        return parse_search(html) if html else []

    def close(self):
        close_quietly(self.session, self.browser)


def check_store(job, store, model):
    """Find and read the product in one store: same ASIN if its page shows the model, else search by model."""
    if load_cooldowns().get(store, 0) > time.time():
        return {"asin": job["asin"], "via": None, "raw": {"status": "blocked (store cooldown)"}}
    client = StoreClient(store, job["cookies"].get(store, ""))
    blocked = {"status": "blocked (captcha)"}
    try:
        raw = client.product(job["asin"])
        if is_blocked(raw):
            set_cooldown(store)
            return {"asin": job["asin"], "via": None, "raw": raw}
        if page_matches(raw, model):
            raw, smid_blocked = prefer_amazon(lambda q: client.product(job["asin"], q), store, job["asin"], raw)
            if smid_blocked:
                set_cooldown(store)
            return {"asin": job["asin"], "via": "same", "raw": raw}
        # A page that could not be read (timeout, error page) says nothing about this store: report it as
        # transient, never as "not listed", so the Worker keeps the quick result / retries later.
        transient = raw.get("status", "").startswith(("error:", "http"))
        if model:
            for hit in client.search(model)[:3]:
                if model_core(model) not in norm(hit["title"]):
                    continue
                raw2 = client.product(hit["asin"])
                if is_blocked(raw2):
                    set_cooldown(store)
                    return {"asin": job["asin"], "via": None, "raw": raw2}
                if page_matches(raw2, model):
                    raw2, smid_blocked = prefer_amazon(lambda q: client.product(hit["asin"], q), store, hit["asin"], raw2)
                    if smid_blocked:
                        set_cooldown(store)
                    return {"asin": hit["asin"], "via": "search", "raw": raw2}
        if transient:
            return {"asin": job["asin"], "via": None, "raw": {"status": raw["status"]}}
        return {"asin": None, "via": None, "raw": {"status": "not listed"}}
    except Blocked:
        set_cooldown(store)
        return {"asin": job["asin"], "via": None, "raw": blocked}
    except Exception as e:  # never log page contents
        return {"asin": job["asin"], "via": None, "raw": {"status": "error: " + type(e).__name__}}
    finally:
        client.close()


def check_job(job):
    """Precise check of a product link for the dashboard: all stores in parallel, one page each (plus search)."""
    t0 = time.time()
    stores = job.get("stores") or list(DOMAIN)
    link = job.get("linkStore") or "it"
    model = job.get("model") or ""
    results = {}
    if not model:  # need the model first to verify the other stores
        results[link] = check_store(job, link, "")
        raw = results[link]["raw"]
        details = raw.get("details") or []
        token = MODEL_TOKEN.search(f'{raw.get("title", "")} {raw.get("pageTitle", "")}'.upper())
        model = details[0] if details else token.group(0) if token else ""
    todo = [st for st in stores if st not in results]
    with ThreadPoolExecutor(max_workers=CHECK_CONCURRENCY) as pool:
        for st, res in zip(todo, pool.map(lambda st: check_store(job, st, model), todo)):
            results[st] = res
    variations = (results.get(link, {}).get("raw") or {}).get("variations") or []
    for res in results.values():  # variations are only needed once
        (res.get("raw") or {}).pop("variations", None)
    print("check", job.get("asin"), "done in", round(time.time() - t0), "s:",
          {st: (r["via"] or r["raw"].get("status")) for st, r in results.items()}, flush=True)
    return {"id": job["id"], "model": model, "variations": variations, "stores": results}


def poll_jobs():
    r = requests.get(WORKER + "/api/job-next", headers=AUTH, timeout=15)
    r.raise_for_status()
    job = r.json()
    if not job.get("id"):
        return False
    result = check_job(job)
    requests.post(WORKER + "/api/job-result", headers=AUTH, json=result, timeout=60).raise_for_status()
    return True


# ------------------------------------------------------------------ schedule
def due_slot(state):
    now = datetime.now(IL)
    settings = state["settings"]
    modes = state.get("modes") or {}   # the Worker's effective mode per day (pre-Prime / Prime schedules)
    for back in range(0, 3):
        day = (now - timedelta(days=back)).date()
        prime = settings.get("primeAuto") and day.isoformat() in state.get("primeDays", [])
        mode = modes.get(day.isoformat()) or ("1h" if prime else settings.get("mode", "3x"))
        hours = state["schedules"].get(mode) or state["schedules"]["3x"]
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


PENDING = {"slot": None, "payload": None, "tries": 0}   # last scanned slot and its unsent payload


def progress(slot, store):
    """Tell the Worker a precise scan is running (it then holds back its light fallback scan)."""
    try:
        requests.post(WORKER + "/api/progress", headers=AUTH, json={"slot": slot, "store": store}, timeout=15)
    except Exception:
        pass


def send_ingest(payload):
    r = requests.post(WORKER + "/api/ingest", headers=AUTH, json=payload, timeout=120)
    print("ingest:", r.status_code, flush=True)
    if r.ok:
        for row in r.json().get("rows", []):
            print("  ", row)
    return r.ok


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
    if not force and slot == PENDING["slot"]:
        # Scanned already but the Worker didn't take it: re-send the same payload, never rescan the slot
        # (a rescan every 2 minutes would hammer Amazon from the home IP).
        if PENDING["payload"] is None or PENDING["tries"] >= MAX_INGEST_RETRIES:
            return 0
        PENDING["tries"] += 1
        ok = send_ingest(PENDING["payload"])
        if ok:
            PENDING["payload"] = None
        return 0 if ok else 1
    print(datetime.now(IL).strftime("%Y-%m-%d %H:%M"), "scanning slot", slot, "(requested)" if requested else "(force)" if FORCE else "", flush=True)
    payload = {"slot": slot, "engine": "home", "version": VERSION, "stores": {}}
    for store in state["stores"]:
        progress(slot, store)
        payload["stores"][store] = scan_store(state, store)
        print(store, "done:", len(payload["stores"][store]["items"]), "items", flush=True)
        time.sleep(REQUEST_DELAY)
    if DRY:
        print("dry run - not sending")
        return 0
    PENDING.update(slot=slot, payload=payload, tries=1)
    ok = send_ingest(payload)
    if ok:
        PENDING["payload"] = None
    return 0 if ok else 1


def job_loop():
    """Product checks from the dashboard - on their own thread, so they also run while a scan is in progress."""
    while True:
        try:
            poll_jobs()
        except Exception as e:
            print("job error:", type(e).__name__, flush=True)
        time.sleep(JOB_POLL)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        print(f"amazon price scanner {VERSION} started (transport={TRANSPORT}; scans checked every 2 minutes, "
              f"product checks every {JOB_POLL}s, {CHECK_CONCURRENCY} stores in parallel)", flush=True)
        threading.Thread(target=job_loop, name="jobs", daemon=True).start()
        while True:
            try:
                main()
            except Exception as e:  # keep the add-on alive on network errors
                print("error:", type(e).__name__, flush=True)
            time.sleep(120)
    sys.exit(main())
