"""A persistent, anonymous Chromium session per Amazon marketplace."""
import json
import os
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait


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
        # Keep Chromium's real user agent, TLS stack, JavaScript and cookie handling.
        options.page_load_strategy = "eager"
        self.driver = webdriver.Chrome(
            service=Service(os.environ.get("CHROMEDRIVER", "/usr/bin/chromedriver")), options=options)
        self.driver.set_page_load_timeout(45)
        try:
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
            self.driver.get(url)
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
            self.driver.get(url)
        except TimeoutException:
            self.driver.execute_script("window.stop()")
            return ""
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
