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

    # ---- seller: prefer Amazon's own offer
    def test_seller_kind(self):
        self.assertEqual(scanner.seller_kind({"seller": "Speditore / Venditore Amazon Amazon"}), "amazon")
        self.assertEqual(scanner.seller_kind({"seller": "Sold by Amazon Export Sales LLC"}), "amazon")
        self.assertEqual(scanner.seller_kind({"seller": "Sold by Direct sales USA",
                                              "buybox": "Ships from: Amazon Sold by: Direct sales USA"}), "other")
        self.assertEqual(scanner.seller_kind({"seller": "Vendu par MBS Merchandise Store"}), "other")
        self.assertEqual(scanner.seller_kind({"seller": ""}), "")

    def test_prefers_amazon_offer_and_keeps_marketplace_offer(self):
        third = {"to": "Israël", "title": "x", "price": "648,70€", "seller": "Vendu par MBS Merchandise Store"}
        own = {"to": "Israël", "title": "x", "price": "651,38€", "seller": "Expéditeur / Vendeur Amazon"}
        get = Mock(return_value=dict(own))
        raw = scanner.prefer_amazon(get, "fr", "B000000001", third)
        get.assert_called_once_with("?smid=A1X6FK5RDHNB96&psc=1")
        self.assertEqual(raw["price"], "651,38€")
        self.assertEqual(raw["alt"]["price"], "648,70€")
        self.assertIn("smid=A1X6FK5RDHNB96", raw["url"])

    def test_keeps_marketplace_offer_without_amazon_offer(self):
        third = {"to": "Israël", "title": "x", "price": "648,70€", "seller": "Vendu par MBS"}
        for other in ({"captcha": True}, {"title": "x", "seller": "Vendu par MBS", "price": "1€"}, {"status": "http 404"}):
            self.assertIs(scanner.prefer_amazon(Mock(return_value=other), "fr", "B000000001", third), third)
        get = Mock()
        amazon = {"price": "1€", "seller": "Sold by Amazon"}
        self.assertIs(scanner.prefer_amazon(get, "us", "B000000001", amazon), amazon)
        get.assert_not_called()

    def test_scan_reads_amazon_offer_for_marketplace_buy_box(self):
        page = GOOD + '<div id="corePrice_feature_div"><span class="a-offscreen">10,00€</span></div>'
        third = scanner.parse(page + '<div id="merchantInfoFeature_feature_div">Venduto da Negozio</div>')
        own = scanner.parse(page + '<div id="merchantInfoFeature_feature_div">Speditore / Venditore Amazon</div>')
        state = {"cookies": {"it": ""}, "products": [{"key": "one", "asinsByStore": {"it": ["B000000001"]}}]}
        with patch.object(scanner, "fetch", side_effect=[(third, ""), (own, "")]) as fetch, \
                patch.object(scanner.time, "sleep"):
            item = scanner.scan_store(state, "it")["items"][0]
        self.assertEqual(fetch.call_args_list[1].args[4], "?smid=A11IL2PNWYJU7H&psc=1")
        self.assertEqual(scanner.seller_kind(item["raw"]), "amazon")
        self.assertIn("alt", item["raw"])

    # ---- precise "add product" check
    def test_model_matching(self):
        self.assertEqual(scanner.model_core("ECAM472.50.B"), "ECAM47250")
        self.assertTrue(scanner.page_matches({"title": "x", "details": ["ECAM 472.50.B"]}, "ECAM472.50.B"))
        self.assertFalse(scanner.page_matches({"title": "Eletta Ultra ECAM472.50.B", "details": ["ECAM450.65.G"]},
                                              "ECAM472.50.B"))
        self.assertTrue(scanner.page_matches({"title": "Eletta Explore", "pageTitle": "ECAM472.50.B - Amazon"},
                                             "ECAM472.50.B"))
        self.assertFalse(scanner.page_matches({"captcha": True, "title": "x"}, "ECAM472.50.B"))

    def test_search_results_skip_accessories(self):
        html = ('<div data-component-type="s-search-result" data-asin="B000000001"><h2>De\'Longhi ECAM472.50.B</h2></div>'
                '<div data-component-type="s-search-result" data-asin="B000000002"><h2>Caraffa compatibile ECAM472</h2></div>'
                '<div data-component-type="s-search-result" data-asin=""><h2>Ad</h2></div>')
        self.assertEqual([h["asin"] for h in scanner.parse_search(html)], ["B000000001"])

    def test_variations(self):
        html = '"dimensionValuesDisplayData" : {"B000000001":["Nero"],"B000000002":["Titanio"]}'
        self.assertEqual(scanner.variations_from(html), [{"asin": "B000000001", "label": "Nero"},
                                                         {"asin": "B000000002", "label": "Titanio"}])

    def test_check_job_searches_when_asin_is_other_product(self):
        wrong = {"title": "Eletta Explore", "details": ["ECAM450.65.G"], "seller": "Sold by Amazon"}
        right = {"title": "Eletta Ultra", "details": ["ECAM472.50.B"], "seller": "Sold by Amazon", "price": "1€"}

        class Client:
            def __init__(self, store, jar):
                self.store = store

            def product(self, asin, query=""):
                return right if (asin == "B000000009" or self.store == "it") else wrong

            def search(self, model):
                return [{"asin": "B000000009", "title": "De'Longhi Eletta Ultra ECAM472.50.B"}]

            def close(self):
                pass

        job = {"id": "j", "asin": "B000000001", "linkStore": "it", "model": "ECAM472.50.B",
               "stores": ["it", "fr"], "cookies": {}}
        with patch.object(scanner, "StoreClient", Client):
            res = scanner.check_job(job)
        self.assertEqual(res["stores"]["it"]["via"], "same")
        self.assertEqual(res["stores"]["fr"], {"asin": "B000000009", "via": "search", "raw": right})

    def test_check_job_respects_cooldown(self):
        scanner.save_cooldowns({"fr": scanner.time.time() + 100})
        with patch.object(scanner, "StoreClient") as client:
            res = scanner.check_job({"id": "j", "asin": "B000000001", "linkStore": "fr", "model": "X1",
                                     "stores": ["fr"], "cookies": {}})
        client.assert_not_called()
        self.assertTrue(res["stores"]["fr"]["raw"]["status"].startswith("blocked"))


if __name__ == "__main__":
    unittest.main()
