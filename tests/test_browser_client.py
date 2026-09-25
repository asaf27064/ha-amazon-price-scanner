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
    def __init__(self, cookies=()):
        self.cookies = list(cookies)

    def set_page_load_timeout(self, seconds):
        pass

    def execute_cdp_cmd(self, method, args):
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
