import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import comment_monitor as cm


class CountCommentBaselineTests(unittest.TestCase):
    def setUp(self):
        self.state = {"baseline_done": True}
        self.args = SimpleNamespace(limit=0, max_pages=10, no_replies=True, platform=[], dry_run=False, no_api=False)

    def item(self, name="甲", count=1, **metadata):
        return {"account_key": f"wechat_service:{name}", "platform": "wechat_service",
                "platform_label": "服务号", "account_name": name, "content_id": "100-1", "title": "文章",
                "stats_comment": count, **metadata}

    def scan(self, *contents):
        new, errors, self.state = cm.check_comments(None, list(contents), self.args, state=self.state)
        self.assertEqual([], errors)
        return new

    def test_same_mid_across_two_accounts_has_independent_growth(self):
        self.assertEqual([], self.scan(self.item("甲", 10), self.item("乙", 90)))
        self.assertEqual({"wechat_service:甲:100-1": 10, "wechat_service:乙:100-1": 90}, self.state["content_counts"])
        new = self.scan(self.item("乙", 90), self.item("甲", 13))
        self.assertEqual([("甲", 3)], [(item["account_name"], item["added_count"]) for item in new])
        self.assertEqual([], self.scan(self.item("甲", 13), self.item("乙", 90)))

    def test_source_or_definition_change_rebases_silently_then_tracks_real_growth(self):
        old = {"data_source": "legacy", "comment_definition": "root_comments"}
        new = {"data_source": "wechat_browser", "comment_definition": "lifetime_visible_comments_including_replies"}
        self.assertEqual([], self.scan(self.item(count=2, **old)))
        self.assertEqual([], self.scan(self.item(count=12, **new)))
        self.assertEqual(2, self.scan(self.item(count=14, **new))[0]["added_count"])
        changed_definition = {**new, "comment_definition": "another_definition"}
        self.assertEqual([], self.scan(self.item(count=25, **changed_definition)))
        self.assertEqual(1, self.scan(self.item(count=26, **changed_definition))[0]["added_count"])

    def test_cached_samples_do_not_change_count_or_provenance(self):
        origin = {"data_source": "wechat_browser", "comment_definition": "comments_and_replies"}
        self.scan(self.item(count=10, **origin))
        before_counts = copy.deepcopy(self.state["content_counts"])
        before_origins = copy.deepcopy(self.state["content_count_origins"])
        for cached in ({"snapshot_state": "cached"}, {"comment_is_cached": True}, {"comment_metric_source": "cached"}):
            with self.subTest(cached=cached):
                self.assertEqual([], self.scan(self.item(count=99, data_source="different", **cached)))
                self.assertEqual(before_counts, self.state["content_counts"])
                self.assertEqual(before_origins, self.state["content_count_origins"])
        self.assertEqual(1, self.scan(self.item(count=11, **origin))[0]["added_count"])

    def test_legacy_unscoped_count_is_not_assigned_to_either_account(self):
        self.state["content_counts"] = {"wechat_service:100-1": 1}
        self.assertEqual([], self.scan(self.item("甲", 10), self.item("乙", 20)))
        self.assertEqual(1, self.state["content_counts"]["wechat_service:100-1"])
        new = self.scan(self.item("甲", 12), self.item("乙", 20))
        self.assertEqual([("甲", 2)], [(item["account_name"], item["added_count"]) for item in new])

    def test_missing_metadata_works_only_with_an_explicit_account_owner(self):
        self.assertEqual([], self.scan(self.item(count=4)))
        self.assertEqual(1, self.scan(self.item(count=5))[0]["added_count"])
        self.assertEqual([], self.scan(self.item(count=50, data_source="new_source")))
        unidentified = {**self.item(count=70), "account_key": ""}
        self.assertEqual([], self.scan(unidentified))
        self.assertNotIn(":100-1", self.state["content_counts"])
        self.assertNotIn("wechat_service:100-1", self.state["content_counts"])

    def test_content_list_keeps_metric_definition_source_and_cache_evidence(self):
        with tempfile.TemporaryDirectory(prefix="promotion-comment-counts-") as tmp:
            video, article = Path(tmp) / "video.json", Path(tmp) / "article.json"
            video.write_text(json.dumps({"videos": []}))
            records = []
            for i, fields in enumerate(({}, {"snapshot_state": "cached"}, {"cached_fields": ["stats.comment"]},
                                       {"metric_provenance": {"comment": {"source": "cached"}}})):
                records.append({"account_key": "wechat_service:甲", "account_name": "甲", "platform": "wechat_service",
                                "article_id": str(i), "published_at": "", "data_source": "wechat_browser", "stats": {"comment": 12},
                                "metric_provenance": {"comment": {"source": "wechat_browser", "definition": "comments_and_replies"}},
                                **fields})
            # IDs cannot be zero; use positive fixture IDs.
            for i, row in enumerate(records, 1):
                row["article_id"] = str(i)
            article.write_text(json.dumps({"articles": records}))
            with patch.object(cm, "VIDEO_DATA", video), patch.object(cm, "ARTICLE_DATA", article):
                contents = cm.build_content_list()
            self.assertEqual("wechat_browser", contents[0]["data_source"])
            self.assertEqual("comments_and_replies", contents[0]["comment_definition"])
            self.assertEqual("wechat_browser", contents[0]["comment_metric_source"])
            self.assertEqual("cached", contents[1]["snapshot_state"])
            self.assertTrue(contents[2]["comment_is_cached"])
            self.assertTrue(contents[3]["comment_is_cached"])
            self.assertEqual([], self.scan(*contents))
            self.assertEqual({"wechat_service:甲:1": 12}, self.state["content_counts"])

    def test_real_comment_api_branch_keeps_its_original_platform_content_key(self):
        item = {**self.item(), "platform": "bilibili", "account_key": "bilibili:甲", "content_id": "BV-fixture",
                "snapshot_state": "cached", "comment_is_cached": True}
        client = SimpleNamespace(call_count=0, fetch_roots=lambda *args: ([{"comment_id": "r1", "reply_count": 0}], 1))
        _, errors, state = cm.check_comments(client, [item], self.args, state=self.state)
        self.assertEqual([], errors)
        self.assertEqual(["r1"], state["seen_comments"]["bilibili:BV-fixture"])
        self.assertIn("bilibili:BV-fixture", state["full_scan_baselines"])
        self.assertEqual({}, state["content_count_origins"])
