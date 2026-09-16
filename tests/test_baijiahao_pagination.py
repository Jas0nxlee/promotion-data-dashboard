import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from fetch_article_data import BaijiahaoCollector


class BaijiahaoPaginationTests(unittest.TestCase):
    account = {"platform": "baijiahao", "account_name": "测试", "platform_uid": "1", "business_line": "测试"}

    def scan(self, pages):
        profile = {"user": {"nickname": "测试", "tabs": [{"url": "https://author.baidu.com/home/1?tab=article"}]}}
        client = Mock()
        client.get.side_effect = [SimpleNamespace(text="window.runtime=" + json.dumps(profile))] + [
            SimpleNamespace(text="window.dynamicData=" + json.dumps(page)) for page in pages]
        return BaijiahaoCollector(client, len(pages)).collect(self.account)

    @staticmethod
    def page(ids, more=True, cursor="next"):
        return {"list": [{"itemType": "article", "itemData": {"article_id": cid, "title": cid}} for cid in ids],
                "hasMore": more, "query": {"ctime": cursor}}

    def test_missing_or_string_end_marker_cannot_be_empty_success(self):
        for value in ({}, {"list": []}, {"list": [], "hasMore": "false"}):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.scan([value])

    def test_terminal_repeated_page_is_partial_and_does_not_claim_complete(self):
        entry, rows = self.scan([self.page(["1"]), self.page(["1"], False)])
        self.assertEqual("partial", entry["status"])
        self.assertEqual(["1"], [row["article_id"] for row in rows])

    def test_missing_empty_or_cyclic_cursor_keeps_partial_history(self):
        variants = [[self.page(["1"]), self.page([], True)],
                    [self.page(["1"], cursor=None)],
                    [self.page(["1"], cursor="a"), self.page(["2"], cursor="b"), self.page(["3"], cursor="a")]]
        for pages in variants:
            with self.subTest(pages=pages):
                entry, rows = self.scan(pages)
                self.assertEqual("partial", entry["status"])
                self.assertTrue(rows)

    def test_explicit_end_after_distinct_pages_preserves_unknown_interactions(self):
        entry, rows = self.scan([self.page(["1"]), self.page(["2"], False)])
        self.assertEqual("ok", entry["status"])
        self.assertEqual(2, len(rows))
        self.assertTrue(all(value is None for row in rows for value in row["stats"].values()))
