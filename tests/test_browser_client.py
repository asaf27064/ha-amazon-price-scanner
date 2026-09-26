import importlib.util
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("browser_client", Path(__file__).parents[1] / "amazon_price_scanner/browser_client.py")
browser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(browser)


class Driver:
    def set_page_load_timeout(self, seconds):
        pass

    current_url = "about:blank"
    ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/131.0.0.0 Safari/537.36"

    def __init__(self, cookies=()):
        self.cookies = list(cookies)
        self.cdp = []
        self.visited = []

    def execute_script(self, script, *args):
        if "navigator.userAgent" in script:
            return self.ua
        if "readyState" in script:
            return "complete"
        if "location.assign" in script:
            self.visited.append(("link", args[0]))
            self.current_url = args[0]
            return None
        if "__leaving" in script:
            return None                                   # the new page has no marker: navigation done
        return None

    def get(self, url):
        self.visited.append(("typed", url))
        self.current_url = url

    def find_elements(self, *a):
        return [1]

    @property
    def page_source(self):
        return "<title>Product</title>"

    def get_cookies(self):
        return []

    def execute_cdp_cmd(self, method, args):
        self.cdp.append((method, args))
        if method in ("Network.setUserAgentOverride", "Emulation.setTimezoneOverride"):
            return {}
        if method == "Network.getCookies":
            return {"cookies": self.cookies}
        if method == "Network.setCookie":
            self.cookies = [c for c in self.cookies if c["name"] != args["name"]]
            self.cookies.append({**args, "session": True})
            return {"success": True}
        raise AssertionError(method)

    def quit(self):
        pass


class BrowserSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def client(self, driver, jar):
        with patch.object(browser.webdriver, "Chrome", return_value=driver):
            return browser.BrowserClient("it", "it", jar, re.compile("session-id|session-token"), self.tmp.name)

    def test_worker_cannot_overwrite_existing_browser_session(self):
        driver = Driver([{"name": "session-id", "value": "local", "domain": ".amazon.it", "path": "/", "session": True}])
        client = self.client(driver, "session-id=foreign-worker-session")
        self.assertEqual(driver.cookies[0]["value"], "local")
        client.close()

    def test_session_cookies_survive_restart_without_reseeding(self):
        first = self.client(Driver(), "session-id=original; session-token=token")
        first.close()
        driver = Driver()
        second = self.client(driver, "session-id=changed-by-cloudflare")
        self.assertEqual({c["name"]: c["value"] for c in driver.cookies},
                         {"session-id": "original", "session-token": "token"})
        second.close()

    def test_missing_session_is_seeded_with_only_allowlisted_cookies(self):
        driver = Driver()
        client = self.client(driver, "session-id=original; auth-token=private")
        self.assertEqual([c["name"] for c in driver.cookies], ["session-id"])
        client.close()

    # ---- 2.0.8
    def test_presents_like_an_ordinary_browser(self):
        driver = Driver()
        client = self.client(driver, "session-id=original")
        ua = [a["userAgent"] for m, a in driver.cdp if m == "Network.setUserAgentOverride"]
        self.assertEqual(len(ua), 1)
        self.assertNotIn("Headless", ua[0])
        self.assertIn("Chrome/131", ua[0])
        self.assertIn(("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Jerusalem"}), driver.cdp)
        client.close()

    def test_home_page_first_after_a_break_then_links(self):
        driver = Driver()
        client = self.client(driver, "session-id=original")
        client.fetch("B000000001", lambda html: {"title": "Product", "captcha": False}, "")
        client.fetch("B000000002", lambda html: {"title": "Product", "captcha": False}, "")
        self.assertEqual(driver.visited, [("typed", "https://www.amazon.it/"),            # cold: the home page first
                                          ("link", "https://www.amazon.it/dp/B000000001"),
                                          ("link", "https://www.amazon.it/dp/B000000002")])
        client.close()
        # a warm profile goes straight to the product, still as a link when a store page is open
        driver2 = Driver()
        driver2.current_url = "https://www.amazon.it/dp/B000000002"
        client2 = self.client(driver2, "session-id=original")
        client2.fetch("B000000003", lambda html: {"title": "Product", "captcha": False}, "")
        self.assertEqual(driver2.visited, [("link", "https://www.amazon.it/dp/B000000003")])
        client2.close()

