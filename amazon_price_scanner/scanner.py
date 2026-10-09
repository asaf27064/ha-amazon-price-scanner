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
from functools import wraps
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
# The site can move to another Cloudflare account: the old copy answers "moved to <url>" and the add-on follows by
# itself (remembered in /data, so a restart keeps it; editing worker_url in the options starts over from that value).
CONFIGURED_WORKER = WORKER
MOVE_FILE = DATA_DIR / "worker_moved.json"


def _site_url(u):
    return isinstance(u, str) and re.fullmatch(r"https://[a-z0-9.-]+", u, re.I) is not None


def load_move():
    global WORKER
    try:
        m = json.loads(MOVE_FILE.read_text())
        if m.get("from") == CONFIGURED_WORKER and _site_url(m.get("to")):
            WORKER = m["to"]
            print("site address (moved):", WORKER, flush=True)
    except Exception:
        pass


def follow_move(answer):
    """A 409 answer from a paused copy of the site: switch to the address it names."""
    global WORKER
    to = (answer or {}).get("movedTo")
    if not _site_url(to) or to.rstrip("/") == WORKER:
        return False
    WORKER = to.rstrip("/")
    print("the site moved - now using", WORKER, flush=True)
    try:
        MOVE_FILE.write_text(json.dumps({"from": CONFIGURED_WORKER, "to": WORKER}))
    except Exception:
        pass
    return True


def moved(response):
    if response.status_code != 409:
        return False
    try:
        return follow_move(response.json())
    except ValueError:
        return False
REQUEST_DELAY = max(5, float(os.environ.get("REQUEST_DELAY", "30")))
# Adaptive pace: after CLEAN_SCANS_TO_SPEED_UP full scans without any challenge the gap between pages shrinks by
# PACE_STEP seconds, down to MIN_REQUEST_DELAY; the first challenge puts it straight back to REQUEST_DELAY.
MIN_REQUEST_DELAY = min(REQUEST_DELAY, max(5, float(os.environ.get("MIN_REQUEST_DELAY", "20"))))
CLEAN_SCANS_TO_SPEED_UP = 3
PACE_STEP = 2.5
BLOCK_COOLDOWN = 3600
# a store's first challenge in a scan: wait this long and reload that page once before pausing the store
CHALLENGE_RETRY = max(0.0, float(os.environ.get("CHALLENGE_RETRY_SECONDS", "90")))
VERSION = "2.1.12"
CHECK_CONCURRENCY = min(6, max(1, int(os.environ.get("CHECK_CONCURRENCY", "3"))))
# Turbo: several stores at the same time (each its own browser and its own gap). The first challenge puts the rest
# of that scan back on the safe path (one page at a time, REQUEST_DELAY apart), and turbo rests for TURBO_REST scans.
TURBO = os.environ.get("TURBO", "false").lower() == "true"
TURBO_PARALLEL = min(3, max(1, int(os.environ.get("TURBO_PARALLEL_STORES", "3"))))
TURBO_DELAY = min(30.0, max(0.0, float(os.environ.get("TURBO_DELAY", "0"))))
TURBO_REST = 3
# How many stores to read at once is learned from the scans themselves (never more than TURBO_PARALLEL): on a small
# host three browsers at once can be far slower than one after another (measured 6-9.10.2026: ~30 s a page with
# three, ~4 s with one). After each clean turbo scan the seconds-per-page of the whole scan is remembered per
# level; a level that hasn't been tried is tried when the pages look choked (or light), and the fastest one stays.
PAR_FORGET = 7 * 86400      # a measurement older than this is forgotten (the host and the pages change)
PAR_CHOKED = 12.0           # seconds to load one page: the browsers are getting in each other's way
PAR_MIN_PAGES = 60          # a short (product-only) scan doesn't teach anything
# A store never gets two pages closer than this - the pace Amazon accepted for weeks (three stores at once, ~7 s a
# page). Without it one store alone would get a page every ~4 s when the stores are read one after another.
TURBO_MIN_INTERVAL = 6.0
JOB_POLL = 10       # one check every 10 s: a product check to run, and a waiting "scan now" (same answer)
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
    image = (img.get("data-old-hires") or "") if img else ""
    if not image.startswith("https://"):                  # the page's image list, else the img src
        m = re.search(r'"hiRes":"(https:[^"]+)"', html) or re.search(r'"large":"(https://m\.media-amazon\.com[^"]+)"', html)
        image = m.group(1) if m else ((img.get("src") or "") if img else "")
    if not image.startswith("https://"):
        image = ""
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
        "pep": txt("#pep-signup-link", 160),
        "promo": txt("#promoPriceBlockMessage_feature_div", 400),   # coupons / checkout discounts / multi-buy     # "join Prime to buy this item at X": a members-only price
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


def title_words(text):
    """Distinctive words of a title (brand, series), for comparing listings across languages."""
    return {w for w in re.findall(r"[a-z0-9]{4,}", clean(text).lower().replace("'", "")) if not w.isdigit()}


def page_matches(raw, model, ref_title=""):
    """Is this product page the product we are looking for? Item-details model numbers first, then the title.
    A page that shows no model number at all only matches when it clearly is the same product as the user's
    link (ref_title): at least two shared distinctive words (e.g. brand + series)."""
    if not raw or raw.get("status") or raw.get("captcha") or not raw.get("title"):
        return False
    if not model:
        return True
    core = model_core(model)
    if raw.get("details"):
        return any(core in norm(d) for d in raw["details"])
    title = f'{raw.get("title", "")} {raw.get("pageTitle", "")}'
    if core in norm(title):
        return True
    if MODEL_TOKEN.search(title.upper()):
        return False                                    # shows a different model number
    return len(title_words(title) & title_words(ref_title)) >= 2


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
ALT_KEYS = ("to", "title", "price", "price2", "pep", "promo", "delivery", "delivery2", "avail", "seller", "returns", "buybox", "global")


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
    except Blocked:
        return raw, True
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


_COOLDOWN_LOCK = threading.RLock()
_STORE_LOCKS = {store: threading.RLock() for store in DOMAIN}
_PAGE_LOCK = threading.Lock()
_NEXT_PAGE_AT = 0.0
_TURBO_LOCKS = {store: threading.Lock() for store in DOMAIN}   # turbo: each store keeps its own queue and pace
_TURBO_NEXT = {store: 0.0 for store in DOMAIN}
_INFLIGHT = {"sem": threading.BoundedSemaphore(TURBO_PARALLEL)}   # turbo: at most this many pages loading at once (set per scan)
_BACKOFF = {"on": False}                                      # a challenge in this scan: back to the safe path
_PAGE_COUNTS = {store: 0 for store in DOMAIN}
_LOAD_SECONDS = {store: 0.0 for store in DOMAIN}
_WAIT_SECONDS = {store: 0.0 for store in DOMAIN}
_CHALLENGE_COUNTS = {store: 0 for store in DOMAIN}
_CHALLENGE_DELAYS = {store: [] for store in DOMAIN}  # the pace in effect when each challenge came
_RETRY_LEFT = {store: True for store in DOMAIN}       # first challenge of a scan: no pause yet, one reload
_SOFT = {store: False for store in DOMAIN}            # set when the last challenge was left without a pause


class Blocked(Exception):
    """This store is cooling down. Do not make a request."""


def load_cooldowns():
    with _COOLDOWN_LOCK:
        try:
            return json.loads((DATA_DIR / "cooldowns.json").read_text())
        except (FileNotFoundError, ValueError):
            return {}


def set_cooldown(store):
    with _COOLDOWN_LOCK:
        cooldowns = load_cooldowns()
        cooldowns[store] = time.time() + BLOCK_COOLDOWN
        save_cooldowns(cooldowns)


def save_cooldowns(cooldowns):
    with _COOLDOWN_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp = DATA_DIR / "cooldowns.tmp"
        temp.write_text(json.dumps(cooldowns))
        temp.replace(DATA_DIR / "cooldowns.json")


_PACE_LOCK = threading.RLock()


def load_pace():
    try:
        pace = json.loads((DATA_DIR / "pace.json").read_text())
    except (FileNotFoundError, ValueError):
        pace = {}
    delay = float(pace.get("delay", REQUEST_DELAY))
    perf = pace.get("perf") if isinstance(pace.get("perf"), dict) else {}
    try:
        par = min(TURBO_PARALLEL, max(1, int(pace.get("par", TURBO_PARALLEL))))
    except (TypeError, ValueError):
        par = TURBO_PARALLEL
    return {"delay": min(REQUEST_DELAY, max(MIN_REQUEST_DELAY, delay)), "clean": int(pace.get("clean", 0)),
            "rest": int(pace.get("rest", 0)), "par": par, "perf": perf}


def save_pace(pace):
    with _PACE_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp = DATA_DIR / f"pace.{threading.get_ident()}.tmp"
        temp.write_text(json.dumps(pace))
        temp.replace(DATA_DIR / "pace.json")


def current_delay():
    return load_pace()["delay"]


def slow_down_now():
    """A challenge: back to the configured (slow) pace right away, for every following page - and out of turbo.
    The next page on the (single) safe queue waits the full safe gap, even if turbo pages were running until now."""
    global _NEXT_PAGE_AT
    _BACKOFF["on"] = True
    _NEXT_PAGE_AT = max(_NEXT_PAGE_AT, time.monotonic() + REQUEST_DELAY)
    try:
        with _PACE_LOCK:
            pace = load_pace()
            pace.update(delay=REQUEST_DELAY, clean=0)
            if TURBO:
                pace["rest"] = TURBO_REST        # also after a challenge in a product check (not only in a scan)
            save_pace(pace)
    except OSError as e:                 # never let a file problem stop the challenge handling (pause, cooldown)
        print("pace: could not save:", type(e).__name__, flush=True)


def turbo_now():
    """Turbo is on, not resting after a recent challenge, and this scan hasn't met a challenge yet."""
    return TURBO and not _BACKOFF["on"] and load_pace()["rest"] == 0


def effective_delay():
    return TURBO_DELAY if turbo_now() else current_delay()


def turbo_parallel():
    """How many stores this turbo scan reads at once (learned; the configured number at most)."""
    return load_pace()["par"]


def tune_parallel(par, pages, elapsed, load):
    """After a clean turbo scan: remember how fast this level was, and choose the level for the next scan."""
    if not TURBO or pages < PAR_MIN_PAGES or elapsed <= 0:
        return None
    with _PACE_LOCK:
        pace = load_pace()
        now = time.time()
        perf = {k: v for k, v in pace["perf"].items()
                if isinstance(v, dict) and now - v.get("t", 0) < PAR_FORGET and k.isdigit() and 1 <= int(k) <= TURBO_PARALLEL}
        perf[str(par)] = {"s": round(elapsed / pages, 2), "t": now}
        per_page = load / pages
        nxt = par
        if par > 1 and str(par - 1) not in perf and per_page > PAR_CHOKED:
            nxt = par - 1                                   # choked: see whether fewer at once is faster
        elif par < TURBO_PARALLEL and str(par + 1) not in perf and per_page < PAR_CHOKED / 2:
            nxt = par + 1                                   # light pages: see whether more at once is faster
        else:
            best = min(perf, key=lambda k: perf[k]["s"])
            if int(best) != par and perf[best]["s"] < perf[str(par)]["s"] * 0.9:
                nxt = int(best)
        pace.update(par=nxt, perf=perf)
        save_pace(pace)
    print("parallel: %d stores at once took %.1f s a page (%.1f s to load one); next scan: %d" % (par, elapsed / pages, per_page, nxt), flush=True)
    return nxt


# ------------------------------------------------------------------ the host (why a scan was slow)
_HOST_PEAK = {}


def host_info():
    """Memory, load and temperature of the machine the add-on runs on (whatever the container lets us read)."""
    info = {"cpus": os.cpu_count()}
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            mem[key] = int(rest.split()[0])
        info["mem_total_mb"] = mem["MemTotal"] // 1024
        info["mem_avail_mb"] = mem.get("MemAvailable", 0) // 1024
        info["swap_used_mb"] = (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) // 1024
    except Exception:
        pass
    try:
        info["load1"] = round(os.getloadavg()[0], 2)
    except Exception:
        pass
    try:
        info["temp_c"] = round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except Exception:
        pass
    return info


def host_sample():
    """Called along the scan: keeps the worst moment (least free memory, most swap, highest load / temperature)."""
    now = host_info()
    for key, pick in (("mem_avail_mb", min), ("swap_used_mb", max), ("load1", max), ("temp_c", max)):
        if key in now:
            _HOST_PEAK[key] = pick(_HOST_PEAK[key], now[key]) if key in _HOST_PEAK else now[key]
    return now


def turbo_after_scan(challenges, read_pages=True):
    """A challenge: turbo rests for TURBO_REST scans. The rest counts down only on a scan without a challenge that
    really read pages (a scan that failed for other reasons says nothing about Amazon's patience)."""
    with _PACE_LOCK:
        pace = load_pace()
        if challenges and TURBO:
            pace["rest"] = TURBO_REST
        elif pace["rest"] > 0 and read_pages:
            pace["rest"] -= 1
        save_pace(pace)
    return pace


def pages_read(stores):
    return sum(1 for d in stores.values() for i in d.get("items", []) if not i["raw"].get("status") and i["raw"].get("title"))


def update_pace(challenges, clean):
    """After a full scan. Any challenge: slow pace. A clean scan (no challenge, no errors / skipped stores, pages
    actually read) counts towards speeding up - several in a row shorten the gap a little (never below
    MIN_REQUEST_DELAY). A scan with errors neither speeds up nor resets: it proves nothing about the pace."""
    with _PACE_LOCK:
        pace = load_pace()
        if challenges:
            pace.update(delay=REQUEST_DELAY, clean=0)
        elif clean:
            pace["clean"] += 1
            if pace["clean"] >= CLEAN_SCANS_TO_SPEED_UP and pace["delay"] > MIN_REQUEST_DELAY:
                pace.update(delay=max(MIN_REQUEST_DELAY, pace["delay"] - PACE_STEP), clean=0)
        save_pace(pace)
    return pace


def scan_was_clean(stores):
    """No challenge, no page errors / skipped / paused stores, and at least one product page really read."""
    items = [i for d in stores.values() for i in d.get("items", [])]
    bad = sum(1 for i in items if str(i["raw"].get("status", "")).startswith(("error:", "skipped", "blocked")))
    good = sum(1 for i in items if not i["raw"].get("status") and i["raw"].get("title"))
    challenges = sum(d.get("stats", {}).get("challenges", 0) for d in stores.values())
    return challenges, challenges == 0 and bad == 0 and good > 0


def serialized_store(fn):
    @wraps(fn)
    def run(state_or_job, store, *args, **kwargs):
        # Checks and scans share a single profile. Never open it twice simultaneously.
        with _STORE_LOCKS[store]:
            return fn(state_or_job, store, *args, **kwargs)
    return run


def _load_page(store, get, pace_now):
    """Open one page and record what it was; a challenge slows everything down and (after one reload) pauses."""
    started = time.monotonic()
    try:
        _PAGE_COUNTS[store] += 1
        value = get()
        raw = value[0] if isinstance(value, tuple) else value
        if isinstance(raw, str):
            raw = parse(raw)
        if isinstance(raw, dict) and is_blocked(raw):
            _CHALLENGE_COUNTS[store] += 1
            _CHALLENGE_DELAYS[store].append(pace_now)
            slow_down_now()
            if CHALLENGE_RETRY and _RETRY_LEFT.get(store):
                _RETRY_LEFT[store] = False      # the caller reloads this page once; a second challenge pauses
                _SOFT[store] = True
            else:
                set_cooldown(store)
        return value
    except Blocked:
        set_cooldown(store)
        slow_down_now()
        raise
    finally:
        _LOAD_SECONDS[store] += time.monotonic() - started


_HANDOVER = object()   # "turbo ended while this page waited - take the safe queue"


def _turbo_page(store, get):
    """Turbo: this store's own queue and gap; a few stores load at the same time. Turbo is checked again right
    before the page is opened (after every wait), so nothing leaves at turbo pace once a challenge was recorded."""
    with _TURBO_LOCKS[store]:
        if load_cooldowns().get(store, 0) > time.time():
            raise Blocked()
        wait = _TURBO_NEXT[store] - time.monotonic()
        if wait > 0:
            time.sleep(wait)
            _WAIT_SECONDS[store] += wait
        if load_cooldowns().get(store, 0) > time.time():
            raise Blocked()
        if not turbo_now():
            return _HANDOVER
        with _INFLIGHT["sem"]:
            if not turbo_now():             # a challenge while this page waited for a free slot
                return _HANDOVER
            started = time.monotonic()
            try:
                return _load_page(store, get, TURBO_DELAY)
            finally:
                _TURBO_NEXT[store] = max(started + TURBO_MIN_INTERVAL, time.monotonic() + TURBO_DELAY) + random.uniform(0, 1)


def amazon_page(store, get):
    """One shared queue for ALL product, seller and search page requests (in turbo: one queue per store).

    Re-check cooldown after waiting: another thread may have just seen a challenge.
    Record a challenge before releasing the queue, so waiting requests see it.
    """
    global _NEXT_PAGE_AT
    if turbo_now():
        value = _turbo_page(store, get)
        if value is not _HANDOVER:
            return value
    with _PAGE_LOCK:
        if load_cooldowns().get(store, 0) > time.time():
            raise Blocked()
        deadline = _NEXT_PAGE_AT
        while True:
            wait = deadline - time.monotonic()
            if wait > 0:
                time.sleep(wait)
                _WAIT_SECONDS[store] += wait
            if _NEXT_PAGE_AT <= deadline:
                break
            deadline = _NEXT_PAGE_AT         # a challenge meanwhile pushed the next safe moment later
        if load_cooldowns().get(store, 0) > time.time():
            raise Blocked()
        try:
            # the gap this page followed (only a challenge changes it within a scan)
            return _load_page(store, get, current_delay())
        finally:
            _NEXT_PAGE_AT = time.monotonic() + current_delay() + random.uniform(0, 3)


def read_product(session, browser, store, asin, jar, query=""):
    return amazon_page(store, lambda: browser.fetch(asin, parse, jar, query) if browser is not None
                       else fetch(session, store, asin, jar, query))


def close_quietly(*things):
    for thing in things:
        try:
            if thing is not None:
                thing.close()
        except Exception:
            pass


@serialized_store
def scan_store(state, store, diagnostic=False):
    jar = state["cookies"].get(store, "")
    items = []
    blocked = False
    aborted = False
    errors_in_row = 0
    cooling = load_cooldowns().get(store, 0) > time.time()
    browser = None
    session = requests.Session()
    initial_pages, initial_challenges = _PAGE_COUNTS[store], _CHALLENGE_COUNTS[store]
    initial_load, initial_wait = _LOAD_SECONDS[store], _WAIT_SECONDS[store]
    initial_challenge_delays, delay_start = len(_CHALLENGE_DELAYS[store]), effective_delay()
    parallel = turbo_parallel() if turbo_now() else 1
    _RETRY_LEFT[store], _SOFT[store] = True, False

    def once_with_retry(read):
        """A store's first challenge: wait, then load that page once more (Amazon's robot check usually clears)."""
        value = read()
        raw = value[0] if isinstance(value, tuple) else value
        if is_blocked(raw) and _SOFT.pop(store, False):
            print(store, "challenge; waiting", int(CHALLENGE_RETRY), "s and reloading once", flush=True)
            _SOFT[store] = False
            _WAIT_SECONDS[store] += CHALLENGE_RETRY
            progress_store(store, state="retry")
            time.sleep(CHALLENGE_RETRY)
            progress_store(store, force=False, state="running")
            value = read()
        return value

    def result():
        pages = _PAGE_COUNTS[store] - initial_pages
        stats = {"requested_pages": pages,
                 "challenges": _CHALLENGE_COUNTS[store] - initial_challenges,
                 "skipped_cooldown": sum(i["raw"].get("status") == "blocked (store cooldown)" for i in items),
                 "cooldown_until": load_cooldowns().get(store, 0),
                 "load_seconds": round(_LOAD_SECONDS[store] - initial_load, 1),
                 "wait_seconds": round(_WAIT_SECONDS[store] - initial_wait, 1),
                 "delay_start": delay_start,
                 "challenge_delays": _CHALLENGE_DELAYS[store][initial_challenge_delays:],
                 "parallel": parallel,
                 "delay": effective_delay()}
        print(store, "summary:", stats, flush=True)
        return {"jar": jar, "items": items, "stats": stats}

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
                progress_item(store, p["key"])
                if STOP["on"] and not diagnostic:
                    raw = {"status": "skipped (stopped)"}
                elif blocked or cooling or load_cooldowns().get(store, 0) > time.time():
                    raw = {"status": "blocked (store cooldown)"}
                elif aborted:
                    raw = {"status": "skipped (store aborted)"}
                else:
                    try:
                        if TRANSPORT == "browser":
                            if browser is None:
                                from browser_client import BrowserClient
                                browser = BrowserClient(store, DOMAIN[store], request_cookies(jar, store),
                                                        KEEP_COOKIES, DATA_DIR)
                            if browser.needs_warm_up():
                                # the store's home page first, as its own paced and counted page; a challenge
                                # there is handled exactly like one on a product page (reload once, then pause)
                                home = once_with_retry(lambda: amazon_page(store, lambda: browser.warm_up(parse)))
                                if is_blocked(home):
                                    raise Blocked()
                        elif TRANSPORT != "requests":
                            raise ValueError("unknown transport")
                        raw, candidate_jar = once_with_retry(lambda: read_product(session, browser, store, asin, jar))
                        # Accept updated delivery sessions only on real Israel product pages.
                        if (not is_blocked(raw) and raw.get("title")
                                and re.search(r"israel|israël|israele|ישראל", raw.get("to", ""), re.I)):
                            jar = candidate_jar
                            if not diagnostic and seller_kind(raw) == "other":
                                def get(query, asin=asin):
                                    return read_product(session, browser, store, asin, jar, query)[0]
                                raw, smid_blocked = prefer_amazon(get, store, asin, raw)
                                if smid_blocked:
                                    blocked = True
                                    set_cooldown(store)
                                    print(store, "blocked on the Amazon-offer page; pausing this store for 60 minutes",
                                          flush=True)
                    except Blocked:
                        blocked = True
                        raw = {"status": "blocked (store cooldown)"}
                    except Exception as e:
                        # Do not log exception text: drivers may include HTML or sensitive URLs.
                        raw = {"status": "error: " + type(e).__name__}
                    if is_blocked(raw):
                        blocked = True
                        set_cooldown(store)
                        print(store, "blocked; pausing this store for 60 minutes", flush=True)
                    elif raw.get("status", "").startswith("error:") and raw.get("status") != "error: product page missing":
                        # A missing page (an ASIN this store doesn't have) is a result, not a failure. Only a
                        # driver/network that keeps failing stops the store (not restarted once per product).
                        errors_in_row += 1
                        if errors_in_row >= 2:
                            aborted = True
                            print(store, "two errors in a row; skipping the rest of this store", flush=True)
                    else:
                        errors_in_row = 0
                items.append({"product": p["key"], "asin": asin, "raw": raw})
                progress_item(store, p["key"], done=True)
                print(store, asin, {k: raw.get(k) for k in
                      ("captcha", "to", "price", "price2", "delivery", "status")}, flush=True)
                if diagnostic:
                    return result()
    finally:
        close_quietly(session, browser)
    return result()


# ------------------------------------------------------------------ "add product" check (fast, parallel)
class SearchFailed(Exception):
    """A search page could not be read - says nothing about whether the store sells the product."""


class StoreClient:
    """A product check uses the same store profile and page queue as scheduled scans."""

    def __init__(self, store, jar):
        self.store, self.jar = store, jar
        self.session = requests.Session()
        self.browser = None

    def _browser(self):
        if self.browser is None:
            from browser_client import BrowserClient
            self.browser = BrowserClient(self.store, DOMAIN[self.store], request_cookies(self.jar, self.store),
                                         KEEP_COOKIES, DATA_DIR)
        return self.browser

    def product(self, asin, query=""):
        browser = self._browser() if TRANSPORT == "browser" else None
        raw, candidate = read_product(self.session, browser, self.store, asin, self.jar, query)
        if (not is_blocked(raw) and raw.get("title")
                and re.search(r"israel|israël|israele|ישראל", raw.get("to", ""), re.I)):
            self.jar = candidate
        return raw

    def search(self, model):
        url = f"https://www.amazon.{DOMAIN[self.store]}/s?k={requests.utils.quote(model)}"

        def get():
            if TRANSPORT == "browser":
                return self._browser().get_html(url)
            r = self.session.get(url, headers={"User-Agent": UA, "Accept-Language": LANG[self.store],
                                 "Accept": "text/html", "Cookie": request_cookies(self.jar, self.store)}, timeout=40)
            if r.status_code in (403, 429, 503):
                raise Blocked()
            if r.status_code != 200:
                raise SearchFailed()
            return r.text

        html = amazon_page(self.store, get)
        if not html:
            raise SearchFailed()                        # page didn't load: temporary, not "no results"
        if parse(html).get("captcha"):
            raise Blocked()
        return parse_search(html)

    def close(self):
        close_quietly(self.session, self.browser)


@serialized_store
def check_store(job, store, model, ref_title=None):
    """Find and read the product in one store: same ASIN if its page shows the model, else search by model.
    ref_title=None means this is the store of the user's own link (the page there IS the product)."""
    if load_cooldowns().get(store, 0) > time.time():
        return {"asin": job["asin"], "via": None, "raw": {"status": "blocked (store cooldown)"}}
    client = StoreClient(store, job["cookies"].get(store, ""))
    blocked = {"status": "blocked (captcha)"}
    try:
        raw = client.product(job["asin"])
        if is_blocked(raw):
            set_cooldown(store)
            return {"asin": job["asin"], "via": None, "raw": raw}
        if page_matches(raw, model if ref_title is not None else "", ref_title or ""):
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
                if raw2.get("status", "").startswith(("error:", "http")):
                    transient, raw = True, raw2       # couldn't read a candidate: the store stays "not checked yet"
                    continue
                if page_matches(raw2, model, ref_title or ""):
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
    # the link's own store first: it is the reference (model number and title) for the other stores
    results[link] = check_store(job, link, model)
    raw = results[link]["raw"]
    ref_title = f'{raw.get("title", "")} {raw.get("pageTitle", "")}'
    if not model:
        details = raw.get("details") or []
        token = MODEL_TOKEN.search(ref_title.upper())
        model = details[0] if details else token.group(0) if token else ""
    todo = [st for st in stores if st not in results]
    with ThreadPoolExecutor(max_workers=CHECK_CONCURRENCY) as pool:
        for st, res in zip(todo, pool.map(lambda st: check_store(job, st, model, ref_title), todo)):
            results[st] = res
    variations = (results.get(link, {}).get("raw") or {}).get("variations") or []
    for res in results.values():  # variations are only needed once
        (res.get("raw") or {}).pop("variations", None)
    print("check", job.get("asin"), "done in", round(time.time() - t0), "s:",
          {st: (r["via"] or r["raw"].get("status")) for st, r in results.items()}, flush=True)
    return {"id": job["id"], "model": model, "variations": variations, "stores": results}


def poll_jobs():
    r = requests.get(WORKER + "/api/job-next", headers=AUTH, timeout=15)
    if moved(r):
        return False
    r.raise_for_status()
    job = r.json()
    if "request" in job:                 # the Worker answers the "scan now" question in the same call
        _COMBINED["on"] = True
        wake_for(job.pop("request"))
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
            "request_delay": REQUEST_DELAY, "min_request_delay": MIN_REQUEST_DELAY, "current_delay": current_delay()}
    info["store_stats"] = {}
    results = {}
    for store in state["stores"]:
        data = scan_store(state, store, diagnostic=True)
        info["store_stats"][store] = data["stats"]
        results[store] = [{"asin": i["asin"], **i["raw"]} for i in data["items"]]
    if DRY:
        print(json.dumps({"info": info, "results": results}, ensure_ascii=True))
        return
    r = requests.post(WORKER + "/api/ingest", headers=AUTH,
                      json={"debug": True, "info": info, "results": results}, timeout=60)
    r.raise_for_status()
    print("debug report sent:", r.status_code, flush=True)


PENDING = {"slot": None, "payload": None, "tries": 0, "request": None}   # last scan, its unsent payload, the request it served
FOLLOWUP = {"slot": None, "stores": [], "after": 0.0, "kept": {}}          # stores paused in the last scan, to re-scan


# ------------------------------------------------------------------ live progress (the dashboard shows it)
# Pages read / planned per store, the product being read and the store's state. Sent at most every PROGRESS_EVERY
# seconds (each report is one write on the site), right away when a store starts waiting, finishes or pauses.
# A report also tells the Worker a precise scan is running (it then holds back its light fallback scan).
PROGRESS_EVERY = 12
# "stop the scan" on the dashboard: the Worker answers this scan's progress report with {"stop": true}. No more
# pages are read (the one being loaded finishes); what was read is sent, marked "stopped".
STOP = {"on": False}
_PROG_LOCK = threading.Lock()
PROG = {"slot": None, "only": None, "request": None, "started": 0, "parallel": 1, "stores": {}, "sent": 0.0}


def plan_pages(state, store):
    """How many product pages this scan reads in a store (the progress bar's total)."""
    return sum(len(p["asinsByStore"].get(store) or []) for p in state["products"] if store not in p.get("skip", []))


def progress_start(slot, state, stores, only=None, request=None):
    with _PROG_LOCK:
        PROG.update(slot=slot, only=only, request=request, started=int(time.time()), parallel=turbo_parallel() if turbo_now() else 1,
                    stores={s: {"total": plan_pages(state, s), "done": 0, "state": "waiting", "cur": ""} for s in stores})
    progress_send(force=True)


def progress_store(store, force=True, **fields):
    with _PROG_LOCK:
        if not PROG["slot"] or store not in PROG["stores"]:
            return
        PROG["stores"][store].update(fields)
    progress_send(force=force)


def progress_item(store, product, done=False):
    with _PROG_LOCK:
        x = PROG["stores"].get(store) if PROG["slot"] else None
        if x is None:
            return
        if done:
            x["done"] = min(x["total"], x["done"] + 1)
        else:
            x["cur"] = product
    progress_send()


def progress_end():
    with _PROG_LOCK:
        PROG.update(slot=None, stores={})


def progress_send(force=False):
    with _PROG_LOCK:
        if not PROG["slot"] or (not force and time.time() - PROG["sent"] < PROGRESS_EVERY):
            return
        PROG["sent"] = time.time()
        try:
            host_sample()
        except Exception:
            pass
        running = [s for s, x in PROG["stores"].items() if x["state"] == "running"]
        body = {"slot": PROG["slot"], "store": running[0] if running else "", "v": 2, "started": PROG["started"],
                "only": PROG["only"], "request": PROG["request"], "parallel": PROG["parallel"],
                "stores": {s: dict(x) for s, x in PROG["stores"].items()}}
    if DRY:
        return
    try:
        r = requests.post(WORKER + "/api/progress", headers=AUTH, json=body, timeout=10)
        if r.ok and r.json().get("stop") is True and not STOP["on"]:
            STOP["on"] = True
            print("stop requested on the dashboard - no more pages in this scan", flush=True)
    except Exception:
        pass


def store_finished(store, result):
    paused = result.get("stats", {}).get("cooldown_until", 0) > time.time()
    progress_store(store, state="paused" if paused else "done", cur="")


# a "scan now" pressed on the dashboard wakes the scan loop within seconds (the job thread looks every few polls)
WAKE = threading.Event()
_WOKEN = {"request": None}
_COMBINED = {"on": False}                # the Worker sends the waiting request with the job answer (no extra call)


def wake_for(req):
    if req and req != PENDING["request"] and req != _WOKEN["request"]:
        _WOKEN["request"] = req
        WAKE.set()


def poll_scan_request():
    """Only for an older Worker that doesn't send the request with the job answer."""
    r = requests.get(WORKER + "/api/scan-pending", headers=AUTH, timeout=15)
    wake_for(r.json().get("request") if r.ok else None)


_PARTIAL_LOCK = threading.Lock()


def send_partial(slot, store, data):
    """One store's results right away, so the dashboard shows them during a long scan (display only - the full
    result at the end still does history and alerts). Failures don't matter."""
    with _PARTIAL_LOCK:                  # parallel stores: one at a time, or the site could lose one of them
        try:
            requests.post(WORKER + "/api/ingest-partial", headers=AUTH,
                          json={"slot": slot, "store": store, "items": data["items"]}, timeout=30)
        except Exception:
            pass


def send_ingest(payload):
    r = requests.post(WORKER + "/api/ingest", headers=AUTH, json=payload, timeout=120)
    if moved(r):                         # a result sent to a paused copy: send it to the new address right away
        r = requests.post(WORKER + "/api/ingest", headers=AUTH, json=payload, timeout=120)
    print("ingest:", r.status_code, flush=True)
    if r.ok:
        for row in r.json().get("rows", []):
            print("  ", row)
    return r.ok


def main():
    response = requests.get(WORKER + "/api/state", headers=AUTH, timeout=60)
    if moved(response):
        return 0                         # the next check (seconds away) asks the new address
    response.raise_for_status()
    state = response.json()
    if str(state.get("scanRequest") or "").startswith("debug"):
        run_debug(state)
        return 0
    engine = state["settings"].get("engine", "cloudflare")
    request = state.get("scanRequest") or None           # "scan now" pressed on the dashboard (its timestamp)
    # "scan only this product" (a button per product on the dashboard): the Worker's state then lists just that
    # product - a short scan that is not the slot's scan (the slot's own scan still runs, no follow-up is planned)
    product_scan = " p:" in str(request or "")
    # a request is served once: if its scan couldn't be delivered, it is re-sent, never scanned again
    requested = bool(request) and request != PENDING["request"]
    force = FORCE or requested
    if engine == "cloudflare" and not force:
        return 0                                        # light engine selected - nothing to do
    slot = due_slot(state)
    if PENDING["payload"] is not None:
        # A finished scan the Worker didn't take: re-send the same payload - also when the scan was requested
        # from the dashboard (the request stays open until the Worker accepts a result), and also when the slot
        # already counts as scanned (a follow-up's result). Never rescan Amazon for it: a rescan every 2 minutes
        # would hammer Amazon from the home IP.
        if PENDING["tries"] < MAX_INGEST_RETRIES:
            PENDING["tries"] += 1
            ok = send_ingest(PENDING["payload"])
            if ok:
                PENDING["payload"] = None
            return 0 if ok else 1
        PENDING["payload"] = None                       # gave up on it; a new request may scan again
        if not force:
            return 0
    if FOLLOWUP["stores"] and not requested:
        if slot != FOLLOWUP["slot"]:
            FOLLOWUP["stores"] = []                     # a new slot covers everything anyway
        elif time.time() >= FOLLOWUP["after"]:
            return run_followup(state, slot)
    if not force and slot == state.get("lastScanSlot"):
        return 0                                        # this slot was already scanned
    if not force and slot == PENDING["slot"]:
        return 0                                        # scanned (delivered or given up) - wait for the next slot
    started = datetime.now(IL).strftime("%Y-%m-%d %H:%M")
    print(started, "scanning slot", slot, "(product only)" if product_scan else "(requested)" if requested else "(force)" if FORCE else "", flush=True)
    # "started" and "request": the site keeps a "scan now" pressed while this scan was already running
    payload = {"slot": slot, "engine": "home", "version": VERSION, "started": started, "request": request, "stores": {}}
    _BACKOFF["on"] = False
    STOP["on"] = False
    t0 = time.monotonic()
    only = request.split(" p:", 1)[1].split(",") if product_scan else None
    was_turbo = turbo_now()
    par = turbo_parallel() if was_turbo else 1
    _INFLIGHT["sem"] = threading.BoundedSemaphore(par)
    _HOST_PEAK.clear()
    host_start = host_sample()
    progress_start(slot, state, state["stores"], only, request)

    def one(store):
        progress_store(store, force=False, state="running")
        result = scan_store(state, store)
        store_finished(store, result)
        print(store, "done:", len(result["items"]), "items", flush=True)
        if not DRY:
            send_partial(slot, store, result)
        return store, result
    if was_turbo:
        print("turbo:", par, "stores at a time, gap", TURBO_DELAY, "s per store (at least", TURBO_MIN_INTERVAL, "s between a store's pages)", flush=True)
        with ThreadPoolExecutor(max_workers=par) as pool:
            done = dict(pool.map(one, state["stores"]))
        payload["stores"] = {s: done[s] for s in state["stores"]}
    else:
        for store in state["stores"]:
            payload["stores"][store] = one(store)[1]
    payload["elapsed"] = round(time.monotonic() - t0, 1)
    progress_end()
    stopped = STOP["on"]
    STOP["on"] = False
    if stopped:
        payload["stopped"] = True          # the site keeps what was read and doesn't count this as the slot's scan
    challenges, clean = scan_was_clean(payload["stores"])
    pace = update_pace(challenges, clean and not stopped)   # a stopped scan is never "a clean full scan"
    pace = turbo_after_scan(challenges, pages_read(payload["stores"]) > 0)
    host_sample()
    payload["host"] = {"start": host_start, "worst": dict(_HOST_PEAK), "parallel": par}
    if was_turbo and clean and not stopped and not product_scan:
        stats = [d.get("stats", {}) for d in payload["stores"].values()]
        tune_parallel(par, sum(x.get("requested_pages", 0) for x in stats), payload["elapsed"], sum(x.get("load_seconds", 0) for x in stats))
    print("pace: next gap between pages", pace["delay"], "s", ("(turbo rests for %d scans)" % pace["rest"]) if TURBO and pace["rest"] else "", flush=True)
    if DRY:
        print("dry run - not sending")
        return 0
    PENDING.update(slot=PENDING["slot"] if product_scan else slot, payload=payload, tries=1, request=request)
    if stopped:
        FOLLOWUP["stores"] = []            # stopped by the user: no second visit to paused stores either
    elif not product_scan:
        plan_followup(slot, payload)
    ok = send_ingest(payload)
    if ok:
        PENDING["payload"] = None
    return 0 if ok else 1


def plan_followup(slot, payload):
    """Stores paused by a challenge get one more visit in this slot, right after their pause ends."""
    now = time.time()
    paused = [s for s, d in payload["stores"].items() if d.get("stats", {}).get("cooldown_until", 0) > now]
    if not paused:
        FOLLOWUP["stores"] = []
        return
    FOLLOWUP.update(slot=slot, stores=paused, after=max(payload["stores"][s]["stats"]["cooldown_until"] for s in paused) + 30, kept={})
    print("paused stores", paused, "- will be scanned again after", datetime.fromtimestamp(FOLLOWUP["after"], IL).strftime("%H:%M"), flush=True)


def run_followup(state, slot):
    """Re-scan only the stores that were paused and send just those; the site keeps the other stores as they are."""
    stores = [s for s in FOLLOWUP["stores"] if s in state["stores"]]
    FOLLOWUP["stores"] = []                              # one attempt per slot
    print(datetime.now(IL).strftime("%Y-%m-%d %H:%M"), "scanning the paused stores again:", stores, flush=True)
    payload = {"slot": slot, "engine": "home", "version": VERSION, "started": datetime.now(IL).strftime("%Y-%m-%d %H:%M"),
               "request": None, "stores": {}}
    t0 = time.monotonic()
    STOP["on"] = False
    progress_start(slot, state, stores)
    for store in stores:
        progress_store(store, force=False, state="running")
        payload["stores"][store] = scan_store(state, store)
        store_finished(store, payload["stores"][store])
        print(store, "done:", len(payload["stores"][store]["items"]), "items", flush=True)
        if not DRY:
            send_partial(slot, store, payload["stores"][store])
    payload["elapsed"] = round(time.monotonic() - t0, 1)
    progress_end()
    if STOP["on"]:
        payload["stopped"] = True
    STOP["on"] = False
    challenges, _clean = scan_was_clean({s: payload["stores"][s] for s in stores})
    pace = update_pace(challenges, False)                # a partial scan never counts as a clean full scan
    print("pace: next gap between pages", pace["delay"], "s", flush=True)
    if DRY:
        return 0
    PENDING.update(slot=slot, payload=payload, tries=1)
    ok = send_ingest(payload)
    if ok:
        PENDING["payload"] = None
    return 0 if ok else 1


def job_loop():
    """Product checks from the dashboard - on their own thread, so they also run while a scan is in progress."""
    n = 0
    while True:
        try:
            poll_jobs()
        except Exception as e:
            print("job error:", type(e).__name__, flush=True)
        n += 1
        if not _COMBINED["on"] and n % 2 == 0:
            try:
                poll_scan_request()
            except Exception:
                pass
        time.sleep(JOB_POLL)


if __name__ == "__main__":
    load_move()
    if "--loop" in sys.argv:
        print(f"amazon price scanner {VERSION} started (transport={TRANSPORT}; scans checked every 2 minutes or at once on request, "
              f"product checks every {JOB_POLL}s, {CHECK_CONCURRENCY} stores in parallel)", flush=True)
        threading.Thread(target=job_loop, name="jobs", daemon=True).start()
        while True:
            try:
                main()
            except Exception as e:  # keep the add-on alive on network errors
                print("error:", type(e).__name__, flush=True)
            WAKE.wait(120)                  # every 2 minutes, or right away when "scan now" was pressed
            WAKE.clear()
    sys.exit(main())
