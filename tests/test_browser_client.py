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
        if method in ("Network.setUserAgentOverride", "Emulation.setTimezoneOverride", "Network.enable", "Network.setBlockedURLs"):
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

    # ---- 2.1.11: light pages
    def blocked(self, driver):
        return [a["urls"] for m, a in driver.cdp if m == "Network.setBlockedURLs"]

    def test_light_pages_block_pictures_fonts_and_video_only(self):
        driver = Driver()
        client = self.client(driver, "session-id=original")
        urls = self.blocked(driver)[-1]
        self.assertTrue(client.light)
        self.assertIn("*.jpg*", urls); self.assertIn("*.woff*", urls); self.assertIn("*.mp4*", urls)
        self.assertFalse([u for u in urls if "js" in u or "css" in u or "amazon" in u], "scripts, styles and Amazon's own requests are never blocked")
        client.close()

    def test_a_robot_check_brings_back_full_pages_for_a_day(self):
        driver = Driver()
        client = self.client(driver, "session-id=original")
        raw, _jar = client.fetch("B000000001", lambda html: {"title": "", "captcha": True}, "jar")
        self.assertTrue(raw["captcha"])
        self.assertEqual(self.blocked(driver)[-1], [], "blocking lifted in the same browser (the reload is a full page)")
        self.assertFalse(client.light)
        client.close()
        again = Driver()
        second = self.client(again, "session-id=original")
        self.assertEqual(self.blocked(again), [], "the next scans of this store start with full pages")
        second.close()
        with patch.object(browser.time, "time", return_value=browser.time.time() + 25 * 3600):
            later = Driver()
            third = self.client(later, "session-id=original")
            self.assertTrue(third.light, "a day later light pages are back")
            third.close()

    def test_light_pages_can_be_turned_off(self):
        with patch.object(browser, "LIGHT_PAGES", False):
            driver = Driver()
            client = self.client(driver, "session-id=original")
            self.assertEqual(self.blocked(driver), [])
            client.close()

    def test_an_ordinary_page_reads_the_same_with_light_pages(self):
        driver = Driver()
        client = self.client(driver, "session-id=original")
        raw, _jar = client.fetch("B000000001", lambda html: {"title": "x", "captcha": False, "image": "https://m.media-amazon.com/images/I/a.jpg"}, "jar")
        self.assertEqual(raw["image"], "https://m.media-amazon.com/images/I/a.jpg")
        self.assertTrue(client.light)
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

    def test_home_page_is_a_separate_step_then_pages_are_links(self):
        driver = Driver()
        client = self.client(driver, "session-id=original")
        self.assertTrue(client.needs_warm_up())                                          # cold profile
        home = client.warm_up(lambda html: {"title": "", "captcha": False})
        self.assertEqual(home["transport"], "browser")
        self.assertFalse(client.needs_warm_up())                                         # noted the visit
        client.fetch("B000000001", lambda html: {"title": "Product", "captcha": False}, "")
        client.fetch("B000000002", lambda html: {"title": "Product", "captcha": False}, "")
        self.assertEqual(driver.visited, [("typed", "https://www.amazon.it/"),
                                          ("link", "https://www.amazon.it/dp/B000000001"),
                                          ("link", "https://www.amazon.it/dp/B000000002")])
        client.close()
        # a fresh browser on a recently used profile: no home page, the first product is opened directly
        driver2 = Driver()
        client2 = self.client(driver2, "session-id=original")
        self.assertFalse(client2.needs_warm_up())
        client2.fetch("B000000003", lambda html: {"title": "Product", "captcha": False}, "")
        client2.fetch("B000000004", lambda html: {"title": "Product", "captcha": False}, "")
        self.assertEqual(driver2.visited, [("typed", "https://www.amazon.it/dp/B000000003"),
                                          ("link", "https://www.amazon.it/dp/B000000004")])
        client2.close()

    def test_client_hints_agree_with_the_user_agent(self):
        driver = Driver()
        client = self.client(driver, "session-id=original")
        meta = [a["userAgentMetadata"] for m, a in driver.cdp if m == "Network.setUserAgentOverride"][0]
        self.assertEqual(meta["fullVersion"], "131.0.0.0")
        self.assertIn({"brand": "Chromium", "version": "131"}, meta["brands"])
        self.assertFalse(meta["mobile"])
        client.close()

