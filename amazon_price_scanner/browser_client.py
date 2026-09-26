"""A persistent, anonymous Chromium session per Amazon marketplace - presented like an ordinary desktop browser."""
import json
import os
import re
import time
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait

WARM_UP_AFTER = 3 * 3600      # a store not visited for this long gets its home page first, like a person would
TIMEZONE = "Asia/Jerusalem"


class BrowserClient:
    def __init__(self, store, domain, jar, cookie_filter, data_dir):
        self.origin = f"https://www.amazon.{domain}"
        self.cookie_filter = cookie_filter
        self.profile = Path(data_dir) / "browser" / store
        self.profile.mkdir(parents=True, exist_ok=True)
        options = webdriver.ChromeOptions()
        options.binary_location = os.environ.get("CHROMIUM_BINARY", "/usr/bin/chromium-browser")
        options.add_argument("--headless=new")
        options.add_argument(f"--user-data-dir={self.profile}")
        options.add_argument("--window-size=1365,900")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--no-first-run")
        locale = {"it": "it-IT", "fr": "fr-FR", "es": "es-ES", "de": "de-DE",
                  "uk": "en-GB", "us": "en-US"}[store]
        options.add_argument(f"--lang={locale}")
        options.add_experimental_option("prefs", {"intl.accept_languages": locale})
        # Chromium's namespace sandbox cannot nest inside HA's restricted container.
        # Run as the dedicated scanner user within the add-on's container isolation.
        options.add_argument("--no-sandbox")
        # An ordinary browser: no "controlled by automated software" switch, no navigator.webdriver flag.
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        # Keep Chromium's real TLS stack, JavaScript and cookie handling.
        options.page_load_strategy = "eager"
        self.locale = locale
        self.driver = webdriver.Chrome(
            service=Service(os.environ.get("CHROMEDRIVER", "/usr/bin/chromedriver")), options=options)
        self.driver.set_page_load_timeout(45)
        try:
            self._look_ordinary()
            self._restore_session_cookies()
            existing = self.driver.execute_cdp_cmd("Network.getCookies", {"urls": [self.origin]})
            has_session = any(c["name"] == "session-id" for c in existing.get("cookies", []))
            # Worker cookies seed a NEW profile only. Its light fallback can change
            # the shared jar; that must not overwrite this browser's live session.
            if not has_session:
                for part in jar.split(";"):
                    if "=" not in part:
                        continue
                    name, value = part.strip().split("=", 1)
                    if cookie_filter.fullmatch(name):
                        self.driver.execute_cdp_cmd("Network.setCookie", {
                            "name": name, "value": value, "domain": f".amazon.{domain}",
                            "path": "/", "secure": True})
        except Exception:
            self.close()
            raise

    def _look_ordinary(self):
        """The same Chromium, without the headless label and with the household's time zone."""
        try:
            ua = self.driver.execute_script("return navigator.userAgent") or ""
            if "HeadlessChrome" in ua:
                m = re.search(r"HeadlessChrome/((\d+)[\d.]*)", ua)
                major, full = (m.group(2), m.group(1)) if m else ("120", "120.0.0.0")
                # the client-hint headers must agree with the user agent (Sec-CH-UA and friends)
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride", {
                    "userAgent": ua.replace("HeadlessChrome", "Chrome"),
                    "acceptLanguage": f"{self.locale},{self.locale[:2]};q=0.9,en;q=0.8",
                    "userAgentMetadata": {"brands": [{"brand": "Chromium", "version": major}, {"brand": "Not_A Brand", "version": "24"}],
                                          "fullVersionList": [{"brand": "Chromium", "version": full}, {"brand": "Not_A Brand", "version": "24.0.0.0"}],
                                          "fullVersion": full, "platform": "Linux", "platformVersion": "", "architecture": "x86",
                                          "model": "", "mobile": False, "bitness": "64", "wow64": False}})
            self.driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": TIMEZONE})
        except Exception as e:
            print("browser: could not adjust the presentation:", type(e).__name__, flush=True)

    def _last_visit(self):
        try:
            return float((self.profile / "last-visit").read_text())
        except (FileNotFoundError, ValueError):
            return 0.0

    def _note_visit(self):
        try:
            (self.profile / "last-visit").write_text(str(time.time()))
        except OSError:
            pass

    def _wait_loaded(self):
        try:
            WebDriverWait(self.driver, 15).until(
                lambda d: d.execute_script("return document.readyState") == "complete")
        except TimeoutException:
            pass

    def needs_warm_up(self):
        """A store not visited for a while: a person would open the store first, not a product page."""
        return time.time() - self._last_visit() > WARM_UP_AFTER

    def warm_up(self, parse):
        """The store's home page - one paced, counted page like any other; the caller checks it for a challenge."""
        try:
            self.driver.get(self.origin + "/")
        except TimeoutException:
            self.driver.execute_script("window.stop()")
            return {"status": "error: browser navigation timeout"}
        self._wait_loaded()
        self._note_visit()
        raw = parse(self.driver.page_source)
        raw["transport"] = "browser"
        return raw

    def _navigate(self, url):
        """Open a page the way a person gets there: from the current store page when one is open (so it carries a
        referrer); a fresh browser opens it directly, like a bookmark."""
        on_site = False
        try:
            on_site = str(self.driver.current_url or "").startswith(self.origin)
        except Exception:
            pass
        if on_site:
            try:
                self.driver.execute_script("window.__leaving=1;location.assign(arguments[0])", url)
                WebDriverWait(self.driver, 45).until(lambda d: not d.execute_script("return window.__leaving"))
                self._note_visit()
                return
            except TimeoutException:
                self.driver.execute_script("window.stop()")
                raise
            except Exception:
                pass                                   # fall back to a plain navigation
        self.driver.get(url)
        self._note_visit()

    def _restore_session_cookies(self):
        """Chromium saves persistent cookies itself; retain session cookies on restart too."""
        try:
            saved = json.loads((self.profile / "session-cookies.json").read_text())
        except (FileNotFoundError, ValueError):
            return
        current = self.driver.execute_cdp_cmd("Network.getCookies", {"urls": [self.origin]})["cookies"]
        keys = {(c["name"], c["domain"], c["path"]) for c in current}
        for cookie in saved:
            if (cookie["name"], cookie["domain"], cookie["path"]) not in keys:
                self.driver.execute_cdp_cmd("Network.setCookie", cookie)

    def _save_session_cookies(self):
        cookies = self.driver.execute_cdp_cmd("Network.getCookies", {"urls": [self.origin]})["cookies"]
        fields = ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite")
        session = [{k: c[k] for k in fields if k in c} for c in cookies if c.get("session")]
        temp = self.profile / "session-cookies.tmp"
        temp.write_text(json.dumps(session))
        temp.replace(self.profile / "session-cookies.json")

    def fetch(self, asin, parse, original_jar, query=""):
        url = f"{self.origin}/dp/{asin}{query}"
        try:
            self._navigate(url)
        except TimeoutException:
            # A timed out navigation may still show the previous product. Never use it.
            self.driver.execute_script("window.stop()")
            return {"status": "error: browser navigation timeout"}, original_jar
        try:
            WebDriverWait(self.driver, 15).until(lambda d: d.find_elements(
                "css selector", '#productTitle, form[action*="validateCaptcha"], #captchacharacters'))
        except TimeoutException:
            pass
        # Delivery fragments can arrive after the product title at DOMContentLoaded.
        try:
            WebDriverWait(self.driver, 15).until(
                lambda d: d.execute_script("return document.readyState") == "complete")
        except TimeoutException:
            pass
        raw = parse(self.driver.page_source)
        raw["transport"] = "browser"
        if raw.get("captcha") or not raw.get("title"):
            if not raw.get("captcha"):
                raw["status"] = "error: product page missing"
                raw["pageMessage"] = self.driver.find_element("tag name", "body").text[:500]
            return raw, original_jar
        cookies = self.driver.get_cookies()
        jar = "; ".join(f"{c['name']}={c['value']}" for c in cookies
                        if self.cookie_filter.fullmatch(c["name"]))
        return raw, jar

    def get_html(self, url):
        """Any Amazon page (e.g. search results) after it finished loading."""
        try:
            self._navigate(url)
        except TimeoutException:
            self.driver.execute_script("window.stop()")
            return None                                   # not loaded - the caller treats it as a failure
        try:
            WebDriverWait(self.driver, 15).until(
                lambda d: d.execute_script("return document.readyState") == "complete")
        except TimeoutException:
            pass
        return self.driver.page_source

    def close(self):
        driver = getattr(self, "driver", None)
        if driver:
            try:
                self._save_session_cookies()
            except Exception:
                pass  # preserve the previous snapshot if the driver has already crashed
            self.driver = None
            try:
                driver.quit()
            except Exception:
                pass  # a crashed Chromium must not replace the scan result
