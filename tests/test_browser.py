"""Optional real-browser tests. Install requirements-dev.txt and Playwright Chromium."""
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from radar import Store, Monitor, make_handler

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright, "Optional Playwright dependency not installed")
class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "browser.sqlite3")
        self.store.ingest([
            {"id":"test-ram","title":"Samsung SO-DIMM DDR5 2x8 Go 4800 MHz","price":60},
            {"id":"test-console","title":"Nintendo Switch OLED","price":140},
        ], "import")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.db.close)
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a: None)
        except PermissionError:
            self.skipTest("Environment denies listening sockets")
        self.addCleanup(self.server.server_close)
        self.server.RequestHandlerClass = make_handler(self.store, Monitor(self.store), self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch()
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport={"width":1440,"height":1000})
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}")
        self.page.wait_for_selector(".listing")

    def test_general_profile_favorites_preferences_and_mobile(self):
        p = self.page
        self.assertEqual(p.locator(".listing").count(), 1)
        p.locator('[data-action="new-profile"]').first.click()
        p.locator('[data-template="general"]').click()
        p.locator('[data-key="name"]').fill("Consoles")
        p.locator('[data-key="queries"]').fill("Nintendo Switch")
        # Limit imports to the desired product family, independently from remote query provenance.
        p.get_by_text("Mots-clés et exclusions", exact=True).click()
        p.locator('[data-key="required_all"]').fill("Nintendo, Switch")
        p.locator('#save').click()
        p.wait_for_function("document.querySelector('#dirty').textContent.includes('Tous les réglages')")
        p.locator('nav [data-view="feed"]').click()
        p.wait_for_function("document.querySelectorAll('.listing').length === 2")
        p.locator('[data-favorite="test-console"]').click()
        p.locator('nav [data-view="favorites"]').click()
        p.wait_for_function("document.querySelectorAll('.listing').length === 1")
        self.assertIn("Nintendo", p.locator('.listing-title').inner_text())
        p.locator('nav [data-view="settings"]').click()
        p.locator('[data-appearance="theme"]').select_option('dark')
        p.locator('[data-appearance="density"]').select_option('compact')
        p.locator('#save-settings').click()
        p.wait_for_function("document.querySelector('#settings-dirty').textContent.includes('enregistrées')")
        p.reload()
        p.wait_for_function("document.documentElement.dataset.theme === 'dark'")
        self.assertEqual(p.locator('html').get_attribute('data-density'),'compact')
        p.set_viewport_size({"width":390,"height":844})
        self.assertTrue(p.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
        self.assertEqual(self.errors, [])

    def test_filter_preview_and_no_empty_search_saved(self):
        p = self.page
        p.locator('nav [data-view="profiles"]').click()
        p.get_by_text("Essayer mes filtres sur un titre",exact=True).click()
        p.locator('#preview-title').fill('SO-DIMM DDR5 2x8 Go 4800 MHz')
        p.locator('[data-action="preview"]').click()
        p.wait_for_selector('#preview-result.pass')
        p.locator('#preview-title').fill('DDR4 UDIMM 16 Go')
        p.locator('[data-action="preview"]').click()
        p.wait_for_selector('#preview-result.fail')
        p.locator('[data-key="queries"]').fill('')
        p.locator('#save').click()
        p.wait_for_selector('#toast.error')
        self.assertTrue(self.store.get('config')['profiles'][0]['queries'])
        self.assertEqual(self.errors, [])
