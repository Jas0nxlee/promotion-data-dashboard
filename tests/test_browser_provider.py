"""Real Chromium, entirely routed fixtures: no requests reach a platform."""
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from providers.browser import BrowserSource
from providers.base import ProviderError


class BrowserProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.driver = sync_playwright().start()
        cls.browser = cls.driver.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        self.context = self.browser.new_context(service_workers="block")
        self.responses = [{"code": 0, "data": {"rows": [{"id": "1"}], "more": True}},
                          {"code": 0, "data": {"rows": [{"id": "2"}], "more": False}}]
        self.urls = []

        def route(request):
            url = request.request.url
            self.urls.append(url)
            if urlparse(url).path == "/fixture":
                request.fulfill(content_type="text/html", body='''<!doctype html><button id="next" onclick="load()">下一页</button>
                    <script>let p=0; async function load(){await fetch('/fixture-list?p='+ (++p))};load()</script>''')
            elif urlparse(url).path == "/fixture-list":
                page = int(parse_qs(urlparse(url).query)["p"][0])
                request.fulfill(content_type="application/json", body=json.dumps(self.responses[min(page-1, len(self.responses)-1)]))
            else:
                request.abort()
        self.context.route("**/*", route)
        self.recipe = {"url": "https://www.bilibili.com/fixture", "response_path": "/fixture-list",
                       "rows_path": "data.rows", "has_more_path": "data.more", "next_selector": "#next", "timeout_ms": 2000}
        self.source = BrowserSource({"platform": "bilibili", "account_name": "fixture"}, {"workflows": {"contents": self.recipe}})
        self.source.context = self.context

    def tearDown(self):
        self.context.close()

    def test_browser_clicks_and_captures_two_pages(self):
        result = self.source.pages("contents")
        self.assertTrue(result.complete)
        self.assertEqual(["1", "2"], [r["id"] for r in result.rows])
        self.assertEqual(2, result.count)
        self.assertEqual(0, len(self.context.pages))

    def test_repeated_page_is_failure(self):
        self.responses = [self.responses[0]]
        with self.assertRaisesRegex(ProviderError, "重复页面"):
            self.source.pages("contents")

    def test_missing_completion_marker_is_failure(self):
        del self.responses[0]["data"]["more"]
        with self.assertRaisesRegex(ProviderError, "分页结束标记"):
            self.source.pages("contents")

    def test_comments_cannot_claim_complete_at_page_limit(self):
        with self.assertRaisesRegex(ProviderError, "本轮不推进评论状态"):
            self.source.pages("contents", max_pages=1)

    def test_discovery_can_return_explicit_partial(self):
        result = self.source.pages("contents", max_pages=1, allow_partial=True)
        self.assertFalse(result.complete)
        self.assertEqual(1, len(result.rows))

    def test_empty_page_with_more_is_failure(self):
        self.responses[0]["data"]["rows"] = []
        with self.assertRaisesRegex(ProviderError, "空页面"):
            self.source.pages("contents")

    def test_large_json_id_keeps_precision_in_browser_capture(self):
        self.responses = [{"code": 0, "data": {"rows": [{"id": 12345678901234567890}], "more": False}}]
        result = self.source.pages("contents")
        self.assertEqual(12345678901234567890, result.rows[0]["id"])

    def test_total_count_uses_unique_ids_and_detects_duplicate_page(self):
        self.recipe.pop("has_more_path")
        self.recipe.update({"total_path": "data.total", "row_id_path": "id"})
        self.responses = [{"code": 0, "data": {"rows": [{"id": "1"}], "total": 2}}]
        with self.assertRaisesRegex(ProviderError, "重复页面"):
            self.source.pages("contents")
