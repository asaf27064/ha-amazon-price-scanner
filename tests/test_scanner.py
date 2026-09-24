import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("WORKER_URL", "https://example.invalid")
os.environ.setdefault("UPLOAD_KEY", "test")
spec = importlib.util.spec_from_file_location("scanner", Path(__file__).parents[1] / "coffee_price_scanner/scanner.py")
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)

GOOD = '<title>Product</title><span id="productTitle">Coffee machine</span><span id="glow-ingress-line2">Israele</span>'


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, value in [("DATA_DIR", Path(self.tmp.name)), ("TRANSPORT", "requests")]:
            p = patch.object(scanner, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.state = {"cookies": {"it": "session-id=original"}, "products": [
            {"key": "one", "asinsByStore": {"it": ["B000000001", "B000000002"]}},
            {"key": "two", "asinsByStore": {"it": ["B000000003"]}}]}

    def test_detects_challenge_without_form(self):
        self.assertTrue(scanner.parse('<title>Robot Check</title>')["captcha"])
        self.assertTrue(scanner.parse('<input id="captchacharacters">')["captcha"])
        self.assertFalse(scanner.parse(GOOD)["captcha"])

    def test_replaces_invalid_locale_cookies(self):
        jar = scanner.jar_dict(scanner.request_cookies("session-id=original; lc-acbes=-", "es"))
        self.assertEqual(jar["lc-acbes"], "es_ES")
        self.assertEqual(jar["session-id"], "original")
        self.assertEqual(jar["i18n-prefs"], "EUR")

    def test_challenge_cannot_replace_delivery_cookies(self):
        response = Mock(status_code=503, text='<form action="/errors/validateCaptcha"></form>')
        response.cookies = [Mock(name="session-id", value="bad")]
        raw, jar = scanner.fetch(Mock(get=Mock(return_value=response)), "it", "B000000001", "session-id=original")
        self.assertTrue(raw["captcha"])
        self.assertEqual(jar, "session-id=original")

    def test_stops_store_on_first_challenge_and_persists_cooldown(self):
        with patch.object(scanner, "fetch", return_value=({"captcha": True}, "session-id=bad")) as fetch:
            result = scanner.scan_store(self.state, "it")
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(len(result["items"]), 3)
            self.assertEqual(result["jar"], "session-id=original")
            scanner.scan_store(self.state, "it")
            self.assertEqual(fetch.call_count, 1)
        self.assertGreater(scanner.load_cooldowns()["it"], scanner.time.time())

    def test_rate_limit_stops_store(self):
        with patch.object(scanner, "fetch", return_value=({"status": "http 429"}, "")) as fetch:
            scanner.scan_store(self.state, "it")
            self.assertEqual(fetch.call_count, 1)

    def test_diagnostic_uses_only_one_product(self):
        with patch.object(scanner, "fetch", return_value=(scanner.parse(GOOD), "session-id=new")) as fetch:
            result = scanner.scan_store(self.state, "it", diagnostic=True)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["jar"], "session-id=new")

    def test_foreign_destination_does_not_replace_cookies(self):
        raw = scanner.parse(GOOD.replace("Israele", "Rome"))
        with patch.object(scanner, "fetch", return_value=(raw, "session-id=foreign")):
            result = scanner.scan_store(self.state, "it", diagnostic=True)
        self.assertEqual(result["jar"], "session-id=original")

    def test_delay_between_requests(self):
        with patch.object(scanner, "fetch", return_value=(scanner.parse(GOOD), "session-id=new")), \
                patch.object(scanner.time, "sleep") as sleep:
            scanner.scan_store(self.state, "it")
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(all(c.args[0] >= scanner.REQUEST_DELAY for c in sleep.call_args_list))

    def test_expired_cooldown_allows_next_scan(self):
        scanner.save_cooldowns({"it": scanner.time.time() - 1})
        with patch.object(scanner, "fetch", return_value=(scanner.parse(GOOD), "")) as fetch:
            scanner.scan_store(self.state, "it", diagnostic=True)
        self.assertEqual(fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
