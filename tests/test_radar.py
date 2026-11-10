import copy
import http.client
import http.cookiejar
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from radar import (DEFAULT_CONFIG, DEFAULT_PROFILE, GENERIC_PROFILE, Store, Monitor, ProviderError,
                   VintedProvider, canonical_item, evaluate, make_handler, specs, validate_config, parse_search_url)


def item(title="Samsung SO-DIMM DDR5 2x8 Go 4800 MHz", price=60, **kw):
    return canonical_item(dict(id="123", title=title, price=price, **kw))


class ParsingTests(unittest.TestCase):
    def test_kit_capacity(self):
        result = specs("SO DIMM DDR5 2 × 8 Go 4800 MHz")
        self.assertEqual((result["capacity"], result["count"], result["speed"]), (16, 2, 4800))

    def test_mixed_generations_rejected(self):
        self.assertFalse(evaluate(item("SO-DIMM DDR4 DDR5 16 Go"), DEFAULT_PROFILE)["matched"])

    def test_kit_variants_and_ambiguous_lots(self):
        for title in ["DDR5 2 barrettes de 8 Go", "DDR5 8 Go x 2", "DDR5 16Go (2x8Go)"]:
            self.assertEqual(specs(title)["capacity"], 16)
        self.assertTrue(specs("SO-DIMM DDR5 2x8 Go ou 2x16 Go")["ambiguous"])
        self.assertTrue(specs("SO-DIMM DDR5 2x8 Go et 32 Go")["ambiguous"])

    def test_ambiguous_capacities_rejected(self):
        self.assertFalse(evaluate(item("SO-DIMM DDR5 8 Go ou 16 Go"), DEFAULT_PROFILE)["matched"])

    def test_no_frequency_as_capacity(self):
        self.assertIsNone(specs("SO-DIMM DDR5 5600 MHz")["capacity"])

    def test_target_is_for_correct_capacity_only(self):
        self.assertIsNone(evaluate(item("SO-DIMM DDR5 8 Go"), DEFAULT_PROFILE)["margin"])
        self.assertAlmostEqual(evaluate(item(), DEFAULT_PROFILE)["margin"], 181.3)

    def test_conversion_and_selling_cost_included(self):
        p = dict(DEFAULT_PROFILE, conversion_cost=30, selling_cost=10)
        self.assertAlmostEqual(evaluate(item(), p)["margin"], 141.3)

    def test_unknown_seller_rejected_when_filter_enabled(self):
        self.assertFalse(evaluate(item(), dict(DEFAULT_PROFILE, min_rating=4))["matched"])

    def test_non_ram_and_adapter_exclusions(self):
        for title in ["Adaptateur SO-DIMM DDR5 16 Go", "DDR5 UDIMM 16 Go", "RAM SO-DIMM DDR5 16 Go HS"]:
            self.assertFalse(evaluate(item(title), DEFAULT_PROFILE)["matched"], title)

    def test_word_boundaries(self):
        self.assertTrue(evaluate(item("SO-DIMM DDR5 16 Go HSKing"), DEFAULT_PROFILE)["matched"])

    def test_currency_and_sold(self):
        self.assertFalse(evaluate(item(currency="USD"), DEFAULT_PROFILE)["matched"])
        self.assertFalse(evaluate(item(is_sold=True), DEFAULT_PROFILE)["matched"])

    def test_unknown_speed_when_required(self):
        self.assertFalse(evaluate(item("SO-DIMM DDR5 16 Go"), dict(DEFAULT_PROFILE, min_speed=4800))["matched"])

    def test_margin_gate(self):
        self.assertFalse(evaluate(item(), dict(DEFAULT_PROFILE, margin_only=True, min_margin=200))["matched"])

    def test_invalid_values(self):
        for price in [None, "NaN", "inf", -1]:
            with self.assertRaises(ValueError): item(price=price)
        for url in ["javascript:alert(1)", "https://evil.example", "https://vinted.fr.evil.example/x", "http://vinted.fr/x"]:
            with self.assertRaises(ValueError): item(url=url)

    def test_price_object(self):
        self.assertEqual(item(price={"amount":"62.50", "currency_code":"EUR"})["price"], 62.5)

    def test_config_validation(self):
        self.assertEqual(validate_config(DEFAULT_CONFIG)["interval_seconds"], 300)
        for value in [0, float("nan"), 10, 90000]:
            with self.assertRaises(ValueError): validate_config(dict(DEFAULT_CONFIG, interval_seconds=value))
        c = copy.deepcopy(DEFAULT_CONFIG)
        c["profiles"][1]["id"] = "ddr5"
        with self.assertRaises(ValueError): validate_config(c)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite3")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_dedupe_and_price_drop(self):
        self.assertEqual(self.store.ingest([item()], "import")["alerts"], 1)
        self.assertEqual(self.store.ingest([item()], "import")["alerts"], 0)
        self.assertEqual(self.store.ingest([item(price=55)], "import")["alerts"], 1)
        self.assertEqual(self.store.ingest([item(price=65)], "import")["alerts"], 0)
        self.assertEqual(len(self.store.listing()[0]["history"]), 3)

    def test_import_atomic_validation(self):
        with self.assertRaises(ValueError): self.store.ingest([item(), {"id":"bad"}], "import")
        self.assertEqual(self.store.listing(), [])

    def test_config_recalculates_existing_items(self):
        self.store.ingest([item()], "import")
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["profiles"][0]["max_price"] = 20
        self.store.set("config", config)
        self.assertFalse(self.store.listing()[0]["matches"])

    def test_persistence(self):
        self.store.ingest([item()], "import")
        other = Store(Path(self.temp.name) / "test.sqlite3")
        try: self.assertEqual(len(other.listing()), 1)
        finally: other.db.close()

    def test_monitor_blocks_without_repeated_requests(self):
        class Provider:
            calls = 0
            def search(self, *args, **kwargs):
                self.calls += 1
                raise ProviderError("Refus", blocked=True)
        provider = Provider()
        monitor = Monitor(self.store, provider)
        monitor.control("start")
        monitor.tick()
        monitor.tick()
        self.assertEqual(provider.calls, 1)
        self.assertFalse(monitor.running)
        self.assertIn("Refus", monitor.error)

    def test_rate_limit_backoff_survives_restart_button(self):
        class Provider:
            def search(self, *args, **kwargs): raise ProviderError("429", retry=900)
        monitor = Monitor(self.store, Provider())
        monitor.control("start")
        monitor.tick()
        self.assertGreater(monitor.next_run, time.time()+850)
        next_run = monitor.next_run
        monitor.control("stop")
        monitor.control("start")
        self.assertEqual(next_run, monitor.next_run)

    def test_successful_monitor(self):
        class Provider:
            def search(self, *args, **kwargs): return [item()]
        c = self.store.get("config")
        c["profiles"][0]["queries"] = ["sodimm"]
        self.store.set("config", c)
        monitor = Monitor(self.store, Provider())
        monitor.control("start")
        monitor.tick()
        self.assertIsNotNone(monitor.last_run)
        self.assertEqual(self.store.listing()[0]["source"], "vinted")

    def test_first_scan_silent_then_new_listing_alert(self):
        class Provider:
            items = [item()]
            def search(self, *args, **kwargs): return self.items
        provider = Provider()
        config = self.store.get("config")
        config["profiles"][0]["queries"] = ["sodimm"]
        self.store.set("config",config)
        monitor = Monitor(self.store,provider)
        monitor.control("start"); monitor.tick()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0],0)
        provider.items = [item(), dict(item(),id="new-listing")]
        monitor.next_run=0; monitor.tick()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0],1)
        self.assertEqual(len(self.store.listing()),2)

    def test_pagination_and_source_profile_scope(self):
        class Provider:
            calls=[]
            def search(self, query, profile, **kwargs):
                self.calls.append(kwargs["page"])
                return [dict(item(),id=str(kwargs["page"]))]
        config = self.store.get("config")
        config["profiles"][0]["queries"]=["sodimm"]
        config["pages"]=3
        self.store.set("config",config)
        provider=Provider(); monitor=Monitor(self.store,provider)
        monitor.control("start")
        with patch.object(monitor.stop_event,"wait",return_value=False): monitor.tick()
        self.assertEqual(provider.calls,[1,2,3])
        self.assertEqual(monitor.last_count,3)
        self.assertEqual(self.store.listing()[0]["profile_ids"],["ddr5"])


class ProviderTests(unittest.TestCase):
    def test_missing_connection_blocks(self):
        with self.assertRaises(ProviderError) as raised:
            VintedProvider().search("ram", DEFAULT_PROFILE)
        self.assertTrue(raised.exception.blocked)

    def test_revocation_blocks_collection(self):
        from connection import RemoteProvider
        class Settings:
            def read(self): return {"url":"https://test.owner.workers.dev", "api_key":"rp_"+"a"*64}
        provider=RemoteProvider(Settings())
        error=urllib.error.HTTPError("https://test.owner.workers.dev",403,"",{},None)
        with patch.object(provider.opener,"open",side_effect=error):
            with self.assertRaises(ProviderError) as raised: provider.search("ram",DEFAULT_PROFILE)
        self.assertTrue(raised.exception.blocked)

    def test_key_not_returned_to_dashboard(self):
        from connection import Connection
        with tempfile.TemporaryDirectory() as folder:
            connection=Connection(Path(folder)/"connection.json")
            connection.save("https://test.owner.workers.dev","rp_"+"a"*64)
            self.assertTrue(connection.public()["configured"])
            self.assertNotIn("api_key",connection.public())
            with self.assertRaises(ValueError): connection.save("https://other.owner.workers.dev", "")


class GeneralProfilesTests(unittest.TestCase):
    def profile(self, **kwargs):
        return dict(copy.deepcopy(GENERIC_PROFILE), queries=["Nintendo Switch"], **kwargs)

    def test_general_product_does_not_require_ram(self):
        result = evaluate(item("Nintendo Switch OLED", price=150), self.profile())
        self.assertTrue(result["matched"])
        self.assertEqual(result["specs"], {})
        self.assertIsNone(result["per_gb"])
        self.assertFalse(evaluate(item("Nike Air Max",price=100),self.profile())["matched"])

    def test_general_margin_ignores_ram_capacity(self):
        result = evaluate(item("Nintendo Switch OLED", price=100), self.profile(resale_target=200))
        self.assertAlmostEqual(result["margin"],89.3)

    def test_required_keywords(self):
        profile=self.profile(required_all=["switch"],required_any=["oled","lite"])
        self.assertTrue(evaluate(item("Nintendo Switch OLED"),profile)["matched"])
        self.assertFalse(evaluate(item("Nintendo Switch standard"),profile)["matched"])
        self.assertFalse(evaluate(item("Ecran OLED"),profile)["matched"])

    def test_search_scope(self):
        p=self.profile()
        self.assertFalse(evaluate(item("Nintendo Switch",profile_ids=["another"]),p)["matched"])

    def test_new_schema_and_old_config_migration(self):
        config=validate_config({"interval_seconds":300,"profiles":[self.profile()]})
        self.assertEqual(config["appearance"]["theme"],"light")
        self.assertEqual(config["pages"],1)
        self.assertEqual(config["profiles"][0]["kind"],"general")

    def test_url_import_filters(self):
        result=parse_search_url("https://www.vinted.fr/catalog?search_text=Nintendo+Switch&brand_ids[]=53&catalog_ids=10,20&price_to=120")
        self.assertEqual(result["queries"],["Nintendo Switch"])
        self.assertEqual(result["brand_ids"],[53])
        self.assertEqual(result["catalog_ids"],[10,20])
        self.assertEqual(result["max_price"],120)
        with self.assertRaises(ValueError): parse_search_url("https://evil.example/catalog")

    def test_relative_links_and_new_brand_format(self):
        result=canonical_item({"id":123,"title":"Switch OLED","price":{"amount":"120","currency_code":"EUR"},"url":"/items/123-switch","item_box":{"first_line":"Nintendo"},"photo":{"url":"https://images1.vinted.net/test.webp"}})
        self.assertEqual(result["brand"],"Nintendo")
        self.assertEqual(result["url"],"https://www.vinted.fr/items/123-switch")
        self.assertTrue(result["image"])

    def test_image_host_and_sold_roundtrip(self):
        self.assertEqual(item(image="https://evil.example/a.png")["image"],"")
        self.assertTrue(canonical_item(item(is_sold=True))["sold"])

    def test_invalid_appearance_and_pagination(self):
        for change in [{"pages":0},{"per_page":1000},{"notify_initial":"yes"},{"appearance":{"theme":"evil"}}]:
            with self.assertRaises(ValueError): validate_config(dict(copy.deepcopy(DEFAULT_CONFIG),**change))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.monitor = Monitor(self.store)
        self.handler = make_handler(self.store, self.monitor, 8765)
        self.token = json.load(self.get("/api/state"))["token"]

    def tearDown(self):
        self.store.db.close(); self.temp.cleanup()

    def exchange(self, method, path, data=None, headers=None):
        body = json.dumps(data).encode() if data is not None else b""
        h = {"Host":"127.0.0.1:8765", "Content-Length":str(len(body)), **(headers or {})}
        request = (method + " " + path + " HTTP/1.0\r\n" + "\r\n".join(k+": "+v for k,v in h.items()) + "\r\n\r\n").encode() + body
        handler = self.handler.__new__(self.handler)
        handler.rfile, handler.wfile = io.BytesIO(request), io.BytesIO()
        handler.client_address = ("127.0.0.1", 0)
        handler.handle()
        class Socket:
            def makefile(inner, *args): return io.BytesIO(handler.wfile.getvalue())
        response = http.client.HTTPResponse(Socket())
        response.begin()
        if response.status >= 400:
            raise urllib.error.HTTPError(path, response.status, response.reason, response.headers, response)
        return response

    def get(self, path):
        return self.exchange("GET", path)

    def request(self, path, data, token=True, headers=None):
        h = {"Content-Type":"application/json"}
        if token: h["X-Radar-Token"] = self.token
        h.update(headers or {})
        return self.exchange("POST", path, data, h)

    def test_end_to_end_import_favorite_and_export(self):
        with self.request("/api/import", {"items":[item()]}) as r:
            self.assertEqual(json.load(r)["alerts"], 1)
        with self.request("/api/item", {"id":"123", "field":"favorite", "value":True}): pass
        self.assertTrue(self.store.listing()[0]["favorite"])
        with self.get("/api/export.csv") as r:
            self.assertIn("Samsung", r.read().decode())

    def test_csrf_and_host(self):
        for token, headers in [(False, {}), (True, {"Origin":"https://evil.example"}), (True, {"Host":"evil.example"})]:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.request("/api/control", {"action":"start"}, token, headers)
            self.assertEqual(raised.exception.code, 403)
        self.assertFalse(self.monitor.running)

    def test_invalid_config_and_traversal(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/config", {"profiles":[]})
        self.assertEqual(raised.exception.code,400)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.get("/../radar.py")
        self.assertEqual(raised.exception.code,404)

    def test_profile_preview_and_url_import(self):
        with self.request("/api/preview",{"profile":DEFAULT_PROFILE,"item":{"title":"SO-DIMM DDR5 2x8 Go", "price":60}}) as response:
            self.assertTrue(json.load(response)["matched"])
        self.assertEqual(self.store.listing(),[])
        with self.request("/api/parse-search",{"url":"https://www.vinted.fr/catalog?search_text=Switch&price_to=150"}) as response:
            data=json.load(response)
            self.assertEqual(data["queries"],["Switch"])
            self.assertEqual(data["max_price"],150)

    def test_static_assets(self):
        for path in ["/", "/app.js", "/style.css"]:
            with self.get(path) as r:
                self.assertEqual(r.status,200)
                self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])


class LiveSocketTests(unittest.TestCase):
    def test_local_server(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "test.sqlite3")
            try:
                try:
                    server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a: None)
                except PermissionError:
                    self.skipTest("Environment denies listening sockets; HTTP handler tested in memory.")
                server.RequestHandlerClass = make_handler(store, Monitor(store), server.server_port)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/state") as response:
                        self.assertEqual(json.load(response)["items"], [])
                finally:
                    server.shutdown(); server.server_close(); thread.join()
            finally:
                store.db.close()


if __name__ == "__main__": unittest.main()
