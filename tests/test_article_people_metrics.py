"""Render local assets with routed synthetic data; never request platform data."""
import json
import os
from pathlib import Path
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:18799"


def fixture():
    accounts = [{"account_key": f"{p}:fixture", "account_name": name, "platform": p,
                 "business_line": "芯片", "status": "ok", "covered_articles": 1}
                for p, name in (("wechat_service", "服务号示例"), ("wechat_subscription", "订阅号示例"), ("csdn", "公开平台示例"))]
    specs = [
        (0, "公众号人数完整示例", {"read_users": 1234, "like_users": 0, "share_users": 56, "recommend_users": 8, "root_comments": 3}),
        (1, "公众号人数缺失示例", {"read_users": None, "like_users": None, "root_comments": None}),
        (1, "旧公众号无人数字段", None),
        (2, "公开平台次数示例", {"read_users": 999999}),
    ]
    articles = []
    for index, (account, title, extra) in enumerate(specs):
        row = {**accounts[account], "article_id": str(index), "title": title,
               "published_at": "2026-09-16T09:00:00+08:00", "url": "",
               "stats": {"read": 40 if account == 2 else None, "like": 2 if account == 2 else None,
                         "share": 3 if account == 2 else None, "comment": 7 if index == 0 else None}}
        if extra is not None:
            row["extra_metrics"] = extra
        articles.append(row)
    return {"source": "mock", "updated_at": "2026-09-16T10:00:00+08:00", "accounts": accounts, "articles": articles}


def route_fixture(context, payload):
    assets = {"/articles/index.html": ("web/articles/index.html", "text/html"),
              "/articles/css/style.css": ("web/articles/css/style.css", "text/css"),
              "/articles/js/app.js": ("web/articles/js/app.js", "text/javascript"),
              "/css/style.css": ("web/css/style.css", "text/css"),
              "/lib/echarts.min.js": ("web/lib/echarts.min.js", "text/javascript")}
    def route(request):
        parsed = urlsplit(request.request.url)
        if parsed.netloc != "127.0.0.1:18799":
            request.abort(); return
        if parsed.path == "/articles/data/article_dashboard_data.json":
            request.fulfill(content_type="application/json", body=json.dumps(payload))
        elif parsed.path in assets:
            path, content_type = assets[parsed.path]
            request.fulfill(content_type=content_type, body=(ROOT / path).read_bytes())
        else:
            request.abort()
    context.route("**/*", route)


class ArticlePeopleMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.driver = sync_playwright().start()
        cls.browser = cls.driver.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.driver.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1100}, service_workers="block")
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        route_fixture(self.context, fixture())
        self.page.goto(ORIGIN + "/articles/index.html")
        self.page.wait_for_selector("#detailTable tbody tr")

    def tearDown(self):
        self.context.close()
        self.assertEqual([], self.errors)

    def test_people_values_missing_values_and_original_comment_count_are_separate(self):
        row = self.page.locator("#detailTable tr").filter(has_text="公众号人数完整示例")
        values = row.locator(".mp-people-metrics dd").all_text_contents()
        self.assertEqual(["1,234人", "0人", "56人", "8人", "3条"], values)
        self.assertEqual(["-", "-", "7", "-", "-"], row.locator("td.num").all_text_contents())
        missing = self.page.locator("#detailTable tr").filter(has_text="公众号人数缺失示例")
        self.assertEqual(["-"] * 5, missing.locator(".mp-people-metrics dd").all_text_contents())
        self.assertTrue(self.page.locator("#peopleMetricNote").is_visible())
        self.assertIn("不能相加视为去重人数", self.page.locator("#peopleMetricNote").inner_text())
        if os.environ.get("PROMOTION_ARTICLE_QA_DIR"):
            output = Path(os.environ["PROMOTION_ARTICLE_QA_DIR"])
            output.mkdir(parents=True, exist_ok=True)
            self.page.wait_for_timeout(1000)  # Let the existing KPI/chart animations settle for screenshots.
            self.page.screenshot(path=str(output / "article-people-desktop.png"), full_page=True)
            self.page.locator(".detail-card").screenshot(path=str(output / "article-people-detail.png"))

    def test_people_do_not_enter_totals_rankings_or_other_platform_rows(self):
        self.assertEqual([40, 2, 3], self.page.evaluate("['read','like','share'].map(key => metricSummary(DATA.articles, key).value)"))
        for label, expected in (("总阅读", "40"), ("总点赞", "2"), ("总分享", "3")):
            card = self.page.locator(".kpi").filter(has=self.page.locator(".kpi-label", has_text=label))
            self.assertEqual(expected, card.locator(".kpi-value").get_attribute("data-val"))
        self.assertIn("公开平台次数示例", self.page.locator("#topList").inner_text())
        self.assertNotIn("公众号", self.page.locator("#topList").inner_text())
        for title in ("旧公众号无人数字段", "公开平台次数示例"):
            row = self.page.locator("#detailTable tr").filter(has_text=title)
            self.assertEqual(0, row.locator(".mp-people-metrics").count())

    def test_filter_hides_unrelated_scope_note_and_keeps_mp_unknown_total(self):
        self.page.select_option("#fPlatform", "csdn")
        self.assertFalse(self.page.locator("#peopleMetricNote").is_visible())
        self.assertEqual(0, self.page.locator(".mp-people-metrics").count())
        self.page.select_option("#fPlatform", "wechat_service")
        self.assertTrue(self.page.locator("#peopleMetricNote").is_visible())
        self.assertEqual("", self.page.locator(".kpi").filter(has=self.page.locator(".kpi-label", has_text="总阅读")).locator(".kpi-value").get_attribute("data-val"))
        self.assertEqual("-", self.page.locator(".kpi").filter(has=self.page.locator(".kpi-label", has_text="总阅读")).locator(".kpi-value").inner_text())


if __name__ == "__main__":
    unittest.main()
