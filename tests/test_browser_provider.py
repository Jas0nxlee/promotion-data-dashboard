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

    def test_browser_downloads_csv_and_checks_export_count(self):
        self.context.route("**/fixture-export", lambda route: route.fulfill(content_type="text/html", body='''
            <span id="total">1</span><button id="export" onclick="downloadFile()">导出</button>
            <script>function downloadFile(){const a=document.createElement('a');a.href=URL.createObjectURL(new Blob(['id,views\\n12345678901234567890,42\\n'],{type:'text/csv'}));a.download='export.csv';a.click()}</script>
        '''))
        self.source.settings["workflows"]["export"] = {
            "url": "https://www.bilibili.com/fixture-export", "download_selector": "#export",
            "total_selector": "#total", "coverage": "all_published"}
        result = self.source.export()
        self.assertTrue(result.complete)
        self.assertEqual("12345678901234567890", result.rows[0]["id"])

    def test_explicit_native_201_response_and_request_identity(self):
        self.context.route("**/fixture", lambda route: route.fulfill(content_type="text/html", body='''
            <script>fetch('/fixture-list', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({_log_finder_id:'owner'})})</script>
        '''))
        self.context.route("**/fixture-list", lambda route: route.fulfill(status=201, content_type="application/json",
            body=json.dumps({"errCode": 0, "data": {"rows": [{"id": "one"}], "more": False}})))
        self.recipe.update({"success_http_statuses": [200, 201], "code_path": "errCode",
                            "request_identity_path": "_log_finder_id", "request_identity_value": "owner"})
        self.assertTrue(self.source.pages("contents").complete)
        self.recipe["request_identity_value"] = "someone_else"
        with self.assertRaisesRegex(ProviderError, "账号身份"):
            self.source.pages("contents")

    def test_same_endpoint_from_preview_frame_is_not_the_content_list(self):
        self.context.route("**/fixture", lambda route: route.fulfill(content_type="text/html", body='''
            <iframe name="postCard" src="/preview-frame"></iframe><iframe name="content" src="/content-frame"></iframe>
        '''))
        def frame(route):
            kind = "preview" if "preview-frame" in route.request.url else "content"
            delay = 0 if kind == "preview" else 150
            route.fulfill(content_type="text/html", body=f'''<script>setTimeout(()=>fetch('/fixture-list',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{kind:'{kind}',userpageType:11}})}}),{delay})</script>''')
        self.context.route("**/*-frame", frame)
        self.context.route("**/fixture-list", lambda route: route.fulfill(content_type="application/json",
            body=json.dumps({"code": 0, "data": {"rows": [{"id": route.request.post_data_json["kind"]}], "more": False}})))
        self.recipe.update({"request_frame_name": "content", "request_match": {"userpageType": 11}})
        rows = self.source.pages("contents").rows
        self.assertEqual([{"id": "content"}], rows)
