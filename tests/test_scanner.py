import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("WORKER_URL", "https://example.invalid")
os.environ.setdefault("UPLOAD_KEY", "test")
spec = importlib.util.spec_from_file_location("scanner", Path(__file__).parents[1] / "amazon_price_scanner/scanner.py")
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)

GOOD = '<title>Product</title><span id="productTitle">Product</span><span id="glow-ingress-line2">Israele</span>'


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, value in [("DATA_DIR", Path(self.tmp.name)), ("TRANSPORT", "requests"), ("_NEXT_PAGE_AT", 0.0)]:
            p = patch.object(scanner, target, value)
            p.start()
            self.addCleanup(p.stop)
        sleeper = patch.object(scanner.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
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
            self.assertEqual(fetch.call_count, 2)                      # 2.0.7: one reload, then the pause
            self.assertEqual(len(result["items"]), 3)
            self.assertEqual(result["jar"], "session-id=original")
            scanner.scan_store(self.state, "it")
            self.assertEqual(fetch.call_count, 2)                      # still paused: no new page
        self.assertGreater(scanner.load_cooldowns()["it"], scanner.time.time())

    def test_rate_limit_stops_store(self):
        with patch.object(scanner, "fetch", return_value=({"status": "http 429"}, "")) as fetch:
            scanner.scan_store(self.state, "it")
            self.assertEqual(fetch.call_count, 2)                      # 2.0.7: one reload, then the pause

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

    def test_cooldown_created_during_scan_stops_next_request(self):
        def get(*args):
            scanner.set_cooldown("it")
            return scanner.parse(GOOD), ""
        with patch.object(scanner, "fetch", side_effect=get) as fetch:
            result = scanner.scan_store(self.state, "it")
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["items"][1]["raw"]["status"], "blocked (store cooldown)")

    def test_scan_cooldown_preserves_other_threads_cooldown(self):
        def get(*args):
            scanner.set_cooldown("fr")
            return {"captcha": True}, ""
        with patch.object(scanner, "fetch", side_effect=get):
            scanner.scan_store(self.state, "it")
        self.assertEqual(set(scanner.load_cooldowns()), {"it", "fr"})

    def test_page_queue_is_shared_across_stores(self):
        with patch.object(scanner.time, "monotonic", return_value=100), \
                patch.object(scanner.random, "uniform", return_value=0), \
                patch.object(scanner.time, "sleep") as sleep:
            scanner.amazon_page("it", lambda: {})
            scanner.amazon_page("fr", lambda: {})
        sleep.assert_called_once_with(scanner.REQUEST_DELAY)

    def test_page_queue_rechecks_cooldown_after_wait(self):
        scanner._NEXT_PAGE_AT = 200
        get = Mock(return_value={})
        with patch.object(scanner.time, "monotonic", return_value=100), \
                patch.object(scanner.time, "sleep", side_effect=lambda _: scanner.set_cooldown("it")):
            with self.assertRaises(scanner.Blocked):
                scanner.amazon_page("it", get)
        get.assert_not_called()

    def test_seller_page_uses_full_delay(self):
        with patch.object(scanner, "fetch", return_value=({}, "")), \
                patch.object(scanner.time, "monotonic", return_value=100), \
                patch.object(scanner.random, "uniform", return_value=0), \
                patch.object(scanner.time, "sleep") as sleep:
            scanner.read_product(None, None, "it", "B000000001", "")
            scanner.read_product(None, None, "it", "B000000001", "", "?smid=test")
        sleep.assert_called_once_with(scanner.REQUEST_DELAY)

    def test_store_operations_cannot_open_same_profile_together(self):
        import threading
        started, entered = threading.Event(), threading.Event()
        @scanner.serialized_store
        def operation(job, store):
            entered.set()
        def task():
            started.set()
            operation({}, "it")
        with scanner._STORE_LOCKS["it"]:
            thread = threading.Thread(target=task)
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(entered.wait(0.05))
        thread.join(2)
        self.assertTrue(entered.is_set())

    # ---- seller: prefer Amazon's own offer
    def test_seller_kind(self):
        self.assertEqual(scanner.seller_kind({"seller": "Speditore / Venditore Amazon Amazon"}), "amazon")
        self.assertEqual(scanner.seller_kind({"seller": "Sold by Amazon Export Sales LLC"}), "amazon")
        self.assertEqual(scanner.seller_kind({"seller": "Shipper / Seller Amazon.com Amazon.com"}), "amazon")
        self.assertEqual(scanner.seller_kind({"seller": "Venduto e spedito da Negozio XYZ"}), "other")
        self.assertEqual(scanner.seller_kind({"seller": "Vendu et expédié par Amazon"}), "amazon")
        self.assertEqual(scanner.seller_kind({"buybox": "Ships from and sold by Amazon.com"}), "amazon")
        self.assertEqual(scanner.seller_kind({"seller": "Sold by Direct sales USA",
                                              "buybox": "Ships from: Amazon Sold by: Direct sales USA"}), "other")
        self.assertEqual(scanner.seller_kind({"seller": "Vendu par MBS Merchandise Store"}), "other")
        self.assertEqual(scanner.seller_kind({"seller": ""}), "")

    def test_prefers_amazon_offer_and_keeps_marketplace_offer(self):
        third = {"to": "Israël", "title": "x", "price": "648,70€", "seller": "Vendu par MBS Merchandise Store"}
        own = {"to": "Israël", "title": "x", "price": "651,38€", "seller": "Expéditeur / Vendeur Amazon"}
        get = Mock(return_value=dict(own))
        raw, blocked = scanner.prefer_amazon(get, "fr", "B000000001", third)
        self.assertFalse(blocked)
        get.assert_called_once_with("?smid=A1X6FK5RDHNB96&psc=1")
        self.assertEqual(raw["price"], "651,38€")
        self.assertEqual(raw["alt"]["price"], "648,70€")
        self.assertIn("smid=A1X6FK5RDHNB96", raw["url"])

    def test_keeps_marketplace_offer_without_amazon_offer(self):
        third = {"to": "Israël", "title": "x", "price": "648,70€", "seller": "Vendu par MBS"}
        for other, blocked in (({"captcha": True}, True), ({"title": "x", "seller": "Vendu par MBS", "price": "1€"}, False),
                               ({"status": "http 404"}, False), ({"status": "http 503"}, True)):
            raw, was_blocked = scanner.prefer_amazon(Mock(return_value=other), "fr", "B000000001", third)
            self.assertIs(raw, third)
            self.assertEqual(was_blocked, blocked)
        get = Mock()
        amazon = {"price": "1€", "seller": "Sold by Amazon"}
        self.assertEqual(scanner.prefer_amazon(get, "us", "B000000001", amazon), (amazon, False))
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

    # ---- 1.3.0: robustness
    def test_one_missing_page_does_not_stop_the_store(self):
        good = scanner.parse(GOOD)
        with patch.object(scanner, "fetch", side_effect=[({"status": "error: product page missing"}, ""), (good, ""),
                                                         (good, "")]) as fetch, patch.object(scanner.time, "sleep"):
            items = scanner.scan_store(self.state, "it")["items"]
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual([i["raw"].get("status") for i in items], ["error: product page missing", None, None])

    def test_two_errors_in_a_row_skip_the_rest_without_cooldown(self):
        with patch.object(scanner, "fetch", return_value=({"status": "error: ConnectionError"}, "")) as fetch, \
                patch.object(scanner.time, "sleep"):
            items = scanner.scan_store(self.state, "it")["items"]
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(items[2]["raw"]["status"], "skipped (store aborted)")
        self.assertNotIn("it", scanner.load_cooldowns())

    def test_blocked_amazon_offer_page_pauses_the_store(self):
        page = GOOD + '<div id="corePrice_feature_div"><span class="a-offscreen">10,00€</span></div>'
        third = scanner.parse(page + '<div id="merchantInfoFeature_feature_div">Venduto da Negozio</div>')
        with patch.object(scanner, "fetch", side_effect=[(third, ""), ({"captcha": True}, "")]) as fetch, \
                patch.object(scanner.time, "sleep"):
            items = scanner.scan_store(self.state, "it")["items"]
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(items[0]["raw"]["price"], "10,00€")      # the page that was read is kept
        self.assertEqual(items[1]["raw"]["status"], "blocked (store cooldown)")
        self.assertGreater(scanner.load_cooldowns()["it"], scanner.time.time())

    def test_check_store_reports_unreadable_page_as_transient(self):
        class Client:
            def __init__(self, store, jar):
                pass

            def product(self, asin, query=""):
                return {"status": "error: browser navigation timeout"}

            def search(self, model):
                return []

            def close(self):
                pass

        with patch.object(scanner, "StoreClient", Client):
            res = scanner.check_store({"asin": "B000000001", "cookies": {}}, "fr", "ECAM472.50.B")
        self.assertEqual(res["raw"]["status"], "error: browser navigation timeout")

    def test_check_store_search_captcha_pauses_store(self):
        class Client:
            def __init__(self, store, jar):
                pass

            def product(self, asin, query=""):
                return {"title": "Other", "details": ["XYZ123"]}

            def search(self, model):
                raise scanner.Blocked()

            def close(self):
                pass

        with patch.object(scanner, "StoreClient", Client):
            res = scanner.check_store({"asin": "B000000001", "cookies": {}}, "de", "ECAM472.50.B", "")
        self.assertTrue(res["raw"]["status"].startswith("blocked"))
        self.assertIn("de", scanner.load_cooldowns())

    def test_due_slot_uses_the_workers_mode_per_day(self):
        state = {"settings": {"mode": "2h", "primeAuto": True}, "primeDays": [],
                 "schedules": {"3x": [8, 14, 20], "2h": [8, 10, 12, 14, 16, 18, 20, 22], "1h": list(range(7, 24))},
                 "modes": {"2026-09-29": "2h", "2026-09-28": "3x"}}
        class Clock(scanner.datetime):
            @classmethod
            def now(cls, tz=None):
                return scanner.datetime(2026, 9, 29, 0, 10, tzinfo=scanner.IL)

        with patch.object(scanner, "datetime", Clock):
            self.assertEqual(scanner.due_slot(state), "2026-09-28 20")   # not 22 (yesterday was 3x/day)

    def test_failed_ingest_is_resent_not_rescanned(self):
        state = {"settings": {"engine": "home", "mode": "3x"}, "stores": ["it"], "products": [], "cookies": {},
                 "schedules": {"3x": [8, 14, 20]}, "lastScanSlot": "2026-09-24 14", "scanRequest": None}
        resp = Mock(ok=True)
        resp.json.return_value = state
        scanner.PENDING.update(slot=None, payload=None, tries=0)
        with patch.object(scanner.requests, "get", return_value=resp), \
                patch.object(scanner, "due_slot", return_value="2026-09-24 20"), \
                patch.object(scanner, "scan_store", return_value={"jar": "", "items": []}) as scan, \
                patch.object(scanner, "progress"), patch.object(scanner, "send_partial"), patch.object(scanner.time, "sleep"), \
                patch.object(scanner, "send_ingest", side_effect=[False, False, True]) as send:
            self.assertEqual(scanner.main(), 1)
            self.assertEqual(scanner.main(), 1)
            self.assertEqual(scanner.main(), 0)
            self.assertEqual(scanner.main(), 0)          # payload delivered - nothing more to do
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(send.call_count, 3)
        self.assertEqual(send.call_args.args[0]["version"], scanner.VERSION)

    # ---- 2.0.2: review findings
    def test_page_without_model_needs_a_clear_title_match(self):
        ref = "De'Longhi Eletta Ultra ECAM472.50.B Kaffeevollautomat"
        self.assertFalse(scanner.page_matches({"title": "Kitchen toaster"}, "ECAM472.50", ref))
        self.assertFalse(scanner.page_matches({"title": "De'Longhi Eletta Explore ECAM452.57"}, "ECAM472.50", ref))
        self.assertTrue(scanner.page_matches({"title": "De'Longhi Eletta Ultra macchina da caffè"}, "ECAM472.50", ref))
        carafe_ref = "De'Longhi Rivelia LatteCrema Cool DLSC032 upgrade kit"
        self.assertTrue(scanner.page_matches({"title": "De'Longhi Rivelia LatteCrema Cool Upgrade Kit"}, "DLSC032", carafe_ref))
        self.assertFalse(scanner.page_matches({"title": "Kitchen toaster"}, "ECAM472.50", ""))

    def test_search_that_did_not_load_is_temporary_not_missing(self):
        class Client:
            def __init__(self, store, jar):
                pass

            def product(self, asin, query=""):
                return {"title": "Other", "details": ["XYZ123"]}

            def search(self, model):
                raise scanner.SearchFailed()

            def close(self):
                pass

        with patch.object(scanner, "StoreClient", Client):
            res = scanner.check_store({"asin": "B000000001", "cookies": {}}, "fr", "ECAM472.50.B", "ref title")
        self.assertTrue(res["raw"]["status"].startswith("error:"))
        self.assertNotEqual(res["raw"]["status"], "not listed")

    def test_requested_scan_resends_instead_of_rescanning(self):
        state = {"settings": {"engine": "home", "mode": "3x"}, "stores": ["it"], "products": [], "cookies": {},
                 "schedules": {"3x": [8, 14, 20]}, "lastScanSlot": "2026-09-24 14", "scanRequest": "2026-09-24 20:10"}
        resp = Mock(ok=True)
        resp.json.return_value = state
        scanner.PENDING.update(slot=None, payload=None, tries=0)
        with patch.object(scanner.requests, "get", return_value=resp), \
                patch.object(scanner, "due_slot", return_value="2026-09-24 20"), \
                patch.object(scanner, "scan_store", return_value={"jar": "", "items": []}) as scan, \
                patch.object(scanner, "progress"), patch.object(scanner, "send_partial"), patch.object(scanner.time, "sleep"), \
                patch.object(scanner, "send_ingest", side_effect=[False, True]) as send:
            self.assertEqual(scanner.main(), 1)          # scanned, upload failed
            self.assertEqual(scanner.main(), 0)          # still requested: re-sent, NOT rescanned
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(send.call_count, 2)

    # ---- 2.0.3
    def test_same_request_never_scans_twice_even_after_giving_up(self):
        state = {"settings": {"engine": "home", "mode": "3x"}, "stores": ["it"], "products": [], "cookies": {},
                 "schedules": {"3x": [8, 14, 20]}, "lastScanSlot": "2026-09-24 14", "scanRequest": "2026-09-24 20:10"}
        resp = Mock(ok=True)
        resp.json.return_value = state
        scanner.PENDING.update(slot=None, payload=None, tries=0, request=None)
        with patch.object(scanner.requests, "get", return_value=resp), \
                patch.object(scanner, "due_slot", return_value="2026-09-24 20"), \
                patch.object(scanner, "scan_store", return_value={"jar": "", "items": []}) as scan, \
                patch.object(scanner, "progress"), patch.object(scanner, "send_partial"), patch.object(scanner.time, "sleep"), \
                patch.object(scanner, "send_ingest", return_value=False) as send:
            for _ in range(8):                        # the Worker keeps failing, the old request stays open
                scanner.main()
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(send.call_count, scanner.MAX_INGEST_RETRIES)
            state["scanRequest"] = "2026-09-24 20:40"  # a NEW request from the dashboard scans again
            scanner.main()
            self.assertEqual(scan.call_count, 2)

    def test_search_hit_that_did_not_load_is_temporary(self):
        class Client:
            def __init__(self, store, jar):
                pass

            def product(self, asin, query=""):
                if asin == "B000000009":
                    return {"status": "error: browser navigation timeout"}
                return {"title": "Other", "details": ["XYZ123"]}

            def search(self, model):
                return [{"asin": "B000000009", "title": "De'Longhi ECAM472.50.B"}]

            def close(self):
                pass

        with patch.object(scanner, "StoreClient", Client):
            res = scanner.check_store({"asin": "B000000001", "cookies": {}}, "fr", "ECAM472.50.B", "ref")
        self.assertEqual(res["raw"]["status"], "error: browser navigation timeout")

    def test_same_asin_with_wrong_title_is_kept(self):
        # amazon.fr showed an "Explore" title on the Eletta Ultra's ASIN (Amazon's own mistake): same ASIN, same product
        ref = "De'Longhi Eletta Ultra ECAM472.50.B"
        self.assertTrue(scanner.page_matches({"title": "De'Longhi Eletta Explore Machine à café"}, "ECAM472.50", ref))

    # ---- 2.0.4
    def test_pace_speeds_up_after_clean_scans_and_resets_on_a_challenge(self):
        with patch.object(scanner, "REQUEST_DELAY", 30.0), patch.object(scanner, "MIN_REQUEST_DELAY", 20.0):
            self.assertEqual(scanner.current_delay(), 30.0)
            for _ in range(3):
                scanner.update_pace(0, True)
            self.assertEqual(scanner.current_delay(), 27.5)
            for _ in range(30):
                scanner.update_pace(0, True)
            self.assertEqual(scanner.current_delay(), 20.0)             # never below the floor
            scanner.update_pace(1, False)
            self.assertEqual(scanner.current_delay(), 30.0)             # first challenge: straight back

    def test_scan_sends_each_store_and_does_not_wait_between_stores(self):
        state = {"settings": {"engine": "home", "mode": "3x"}, "stores": ["it", "fr", "es"], "products": [],
                 "cookies": {}, "schedules": {"3x": [8, 14, 20]}, "lastScanSlot": "2026-09-24 14", "scanRequest": None}
        resp = Mock(ok=True)
        resp.json.return_value = state
        scanner.PENDING.update(slot=None, payload=None, tries=0, request=None)
        with patch.object(scanner.requests, "get", return_value=resp), \
                patch.object(scanner, "due_slot", return_value="2026-09-24 20"), \
                patch.object(scanner, "scan_store", return_value={"jar": "", "items": [], "stats": {"challenges": 0}}), \
                patch.object(scanner, "progress"), patch.object(scanner, "send_partial") as partial, \
                patch.object(scanner.time, "sleep") as sleep, patch.object(scanner, "send_ingest", return_value=True):
            scanner.main()
        self.assertEqual([c.args[1] for c in partial.call_args_list], ["it", "fr", "es"])
        sleep.assert_not_called()

    # ---- 2.0.5
    def test_challenge_slows_down_immediately(self):
        with patch.object(scanner, "REQUEST_DELAY", 30.0), patch.object(scanner, "MIN_REQUEST_DELAY", 20.0):
            scanner.save_pace({"delay": 20.0, "clean": 0})
            scanner.amazon_page("it", lambda: ({"captcha": True}, ""))      # a CAPTCHA in the middle of a scan
            self.assertEqual(scanner.current_delay(), 30.0)               # the very next page already waits 30 s
            scanner.save_pace({"delay": 20.0, "clean": 0})

            def blocked():
                raise scanner.Blocked()
            with self.assertRaises(scanner.Blocked):
                scanner.amazon_page("de", blocked)                         # e.g. a product check's search page
            self.assertEqual(scanner.current_delay(), 30.0)

    def test_scans_with_errors_do_not_speed_up(self):
        failed = {"it": {"items": [{"raw": {"status": "error: ConnectionError"}}, {"raw": {"status": "skipped (store aborted)"}}],
                         "stats": {"challenges": 0}}}
        good = {"it": {"items": [{"raw": {"title": "x", "price": "1€"}}], "stats": {"challenges": 0}}}
        self.assertEqual(scanner.scan_was_clean(failed), (0, False))
        self.assertEqual(scanner.scan_was_clean(good), (0, True))
        with patch.object(scanner, "REQUEST_DELAY", 30.0), patch.object(scanner, "MIN_REQUEST_DELAY", 20.0):
            scanner.save_pace({"delay": 30.0, "clean": 0})
            for _ in range(3):
                scanner.update_pace(*scanner.scan_was_clean(failed))
            self.assertEqual(scanner.current_delay(), 30.0)

    # ---- 2.0.6
    def test_challenge_records_the_pace_it_came_at(self):
        with patch.object(scanner, "REQUEST_DELAY", 30.0), patch.object(scanner, "MIN_REQUEST_DELAY", 20.0):
            scanner.save_pace({"delay": 22.5, "clean": 0})
            before = len(scanner._CHALLENGE_DELAYS["uk"])
            scanner.amazon_page("uk", lambda: ({"captcha": True}, ""))
            self.assertEqual(scanner._CHALLENGE_DELAYS["uk"][before:], [22.5])   # not the 30 it was reset to
            self.assertEqual(scanner.current_delay(), 30.0)

    # ---- 2.0.7
    def test_first_challenge_is_reloaded_once_before_pausing(self):
        good = ({"title": "Product", "to": "Israele", "price": "1€", "captcha": False}, "session-id=original")
        with patch.object(scanner, "fetch", side_effect=[({"captcha": True}, "session-id=bad"), good, good, good]) as fetch, \
                patch.object(scanner.time, "sleep") as sleep:
            result = scanner.scan_store(self.state, "it")
        self.assertEqual(fetch.call_count, 4)                          # captcha, reload ok, then the other two pages
        self.assertEqual([i["raw"].get("price") for i in result["items"]], ["1€", "1€", "1€"])
        self.assertEqual(result["stats"]["challenges"], 1)
        self.assertNotIn("it", scanner.load_cooldowns())               # no pause
        self.assertIn(scanner.CHALLENGE_RETRY, [c.args[0] for c in sleep.call_args_list])

    def test_paused_stores_are_scanned_again_after_the_pause(self):
        state = {"settings": {"engine": "home", "mode": "3x"}, "stores": ["it", "fr", "uk"], "products": [],
                 "cookies": {}, "schedules": {"3x": [8, 14, 20]}, "lastScanSlot": "2026-09-26 08", "scanRequest": None}
        resp = Mock(ok=True)
        resp.json.return_value = state
        scanner.PENDING.update(slot="2026-09-26 08", payload=None, tries=1, request=None)
        kept = {"uk": {"jar": "", "items": [{"product": "x", "asin": "A", "raw": {"title": "t", "price": "1"}}], "stats": {"challenges": 0, "cooldown_until": 0}}}
        scanner.FOLLOWUP.update(slot="2026-09-26 08", stores=["it", "fr"], after=scanner.time.time() - 1, kept=kept)
        sent = []
        with patch.object(scanner.requests, "get", return_value=resp), patch.object(scanner, "due_slot", return_value="2026-09-26 08"), \
                patch.object(scanner, "scan_store", return_value={"jar": "", "items": [{"product": "x", "asin": "B", "raw": {"title": "t", "price": "2"}}], "stats": {"challenges": 0, "cooldown_until": 0}}) as scan, \
                patch.object(scanner, "progress"), patch.object(scanner, "send_partial"), \
                patch.object(scanner, "send_ingest", side_effect=lambda p: sent.append(p) or True):
            scanner.main()
        self.assertEqual([c.args[1] for c in scan.call_args_list], ["it", "fr"])   # only the paused stores
        self.assertEqual(sorted(sent[0]["stores"]), ["fr", "it", "uk"])             # sent with the kept results
        self.assertNotIn("kept", sent[0]["stores"]["it"])
        self.assertEqual(sent[0]["slot"], "2026-09-26 08")
        self.assertEqual(scanner.FOLLOWUP["stores"], [])
        # a scan whose stores got paused plans the follow-up
        payload = {"stores": {"it": {"stats": {"cooldown_until": scanner.time.time() + 3000}}, "uk": {"stats": {"cooldown_until": 0}}}}
        scanner.plan_followup("2026-09-26 14", payload)
        self.assertEqual(scanner.FOLLOWUP["stores"], ["it"])
        self.assertEqual(list(scanner.FOLLOWUP["kept"]), ["uk"])
        self.assertTrue(scanner.FOLLOWUP["kept"]["uk"]["kept"])                       # marked, with its own time
        self.assertRegex(scanner.FOLLOWUP["kept"]["uk"]["observed"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertGreater(scanner.FOLLOWUP["after"], scanner.time.time() + 3000)
        scanner.FOLLOWUP.update(slot=None, stores=[], after=0.0, kept={})

    # ---- 2.0.9
    def test_home_page_goes_through_the_paced_queue_and_its_challenge_pauses_the_store(self):
        import types
        calls = []

        class FakeBrowser:
            def __init__(self, *a):
                self.warm = True

            def needs_warm_up(self):
                return self.warm

            def warm_up(self, parse):
                calls.append("home")
                self.warm = False
                return {"captcha": True, "title": ""}

            def fetch(self, asin, parse, jar, query=""):
                calls.append("product")
                return {"title": "Product", "to": "Israele", "price": "1€", "captcha": False}, jar

            def close(self):
                pass
        fake = types.ModuleType("browser_client")
        fake.BrowserClient = FakeBrowser
        with patch.dict(scanner.sys.modules, {"browser_client": fake}), patch.object(scanner, "TRANSPORT", "browser"), \
                patch.object(scanner.time, "sleep") as sleep:
            result = scanner.scan_store(self.state, "it")
        self.assertEqual(calls, ["home", "home"])                       # home, reload once, no product page
        self.assertEqual(result["stats"]["requested_pages"], 2)
        self.assertEqual(result["stats"]["challenges"], 2)
        self.assertGreaterEqual(result["stats"]["wait_seconds"], scanner.CHALLENGE_RETRY)   # the wait is accounted
        self.assertTrue(all(i["raw"]["status"] == "blocked (store cooldown)" for i in result["items"]))
        self.assertGreater(scanner.load_cooldowns()["it"], scanner.time.time())
        self.assertIn(scanner.CHALLENGE_RETRY, [c.args[0] for c in sleep.call_args_list])

    def test_undelivered_followup_is_resent_even_when_the_slot_counts_as_scanned(self):
        state = {"settings": {"engine": "home", "mode": "3x"}, "stores": ["it"], "products": [], "cookies": {},
                 "schedules": {"3x": [8, 14, 20]}, "lastScanSlot": "2026-09-26 08", "scanRequest": None}
        resp = Mock(ok=True)
        resp.json.return_value = state
        payload = {"slot": "2026-09-26 08", "stores": {}}
        scanner.PENDING.update(slot="2026-09-26 08", payload=payload, tries=1, request=None)
        scanner.FOLLOWUP.update(slot=None, stores=[], after=0.0, kept={})
        with patch.object(scanner.requests, "get", return_value=resp), patch.object(scanner, "due_slot", return_value="2026-09-26 08"), \
                patch.object(scanner, "scan_store") as scan, patch.object(scanner, "send_ingest", return_value=True) as send:
            scanner.main()
        send.assert_called_once_with(payload)
        scan.assert_not_called()
        self.assertIsNone(scanner.PENDING["payload"])

    def test_partial_scan_never_counts_as_clean(self):
        with patch.object(scanner, "REQUEST_DELAY", 30.0), patch.object(scanner, "MIN_REQUEST_DELAY", 20.0):
            scanner.save_pace({"delay": 30.0, "clean": 2})
            state = {"stores": ["it"], "products": [], "cookies": {}}
            scanner.FOLLOWUP.update(slot="s", stores=["it"], after=0.0, kept={})
            with patch.object(scanner, "scan_store", return_value={"jar": "", "items": [{"product": "x", "asin": "A", "raw": {"title": "t", "price": "1"}}], "stats": {"challenges": 0, "cooldown_until": 0}}), \
                    patch.object(scanner, "progress"), patch.object(scanner, "send_partial"), patch.object(scanner, "send_ingest", return_value=True):
                scanner.run_followup(state, "s")
            self.assertEqual(scanner.load_pace(), {"delay": 30.0, "clean": 2})    # unchanged: not a full clean scan


if __name__ == "__main__":
    unittest.main()
