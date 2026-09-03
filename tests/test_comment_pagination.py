import sys
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import comment_monitor as cm


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, params=None, payload=None, **_kwargs):
        self.calls.append({"method": method, "path": path,
                           "params": params, "payload": payload})
        if not self.responses:
            raise AssertionError("测试响应不足")
        return self.responses.pop(0)


def item(platform="douyin"):
    return {"platform": platform, "content_id": "content-1", "aid": ""}


class CommentPaginationTests(unittest.TestCase):
    @staticmethod
    def args(**overrides):
        values = {
            "limit": 0, "max_pages": 10, "no_replies": True,
            "platform": [], "dry_run": False, "no_api": False,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    @staticmethod
    def content():
        return {
            "account_key": "douyin:account", "platform": "douyin",
            "platform_label": "抖音", "account_name": "account",
            "content_id": "content-1", "aid": "", "title": "title",
            "url": "https://example.com", "published_at": "2026-09-01T00:00:00+08:00",
            "content_type": "视频", "stats_comment": 1,
        }

    def test_douyin_follows_cursor_until_complete(self):
        client = FakeClient([
            {"data": {"comments": [{"cid": "1", "text": "a"}],
                       "has_more": 1, "cursor": 20}},
            {"data": {"comments": [{"cid": "2", "text": "b"}],
                       "has_more": 0, "cursor": 40}},
        ])
        comments, stats = cm.fetch_all_comments(
            client, "douyin", item(), max_pages=10, include_replies=False)
        self.assertEqual(["1", "2"], [x["comment_id"] for x in comments])
        self.assertEqual(2, stats["root_pages"])
        self.assertEqual(20, client.calls[1]["params"]["cursor"])

    def test_bilibili_paginates_roots_and_second_level_replies(self):
        client = FakeClient([
            {"data": {"replies": [{"rpid": "r1", "rcount": 2,
                                      "content": {"message": "root"}}],
                       "cursor": {"is_end": False,
                                  "next": 2,
                                  "pagination_reply": {"next_offset": "internal-token"}}}},
            {"data": {"replies": [{"rpid": "r2", "rcount": 0,
                                      "content": {"message": "root2"}}],
                       "cursor": {"is_end": True}}},
            {"data": {"root": {"replies": [
                {"rpid": "c1", "content": {"message": "child1"}}]},
                "cursor": {"is_end": False,
                           "next": 1,
                           "pagination_reply": {"next_offset": "internal-child-token"}}}},
            {"data": {"root": {"replies": [
                {"rpid": "c2", "content": {"message": "child2"}}]},
                "cursor": {"is_end": True}}},
        ])
        comments, stats = cm.fetch_all_comments(
            client, "bilibili", item("bilibili"), max_pages=10)
        self.assertEqual({"r1", "r2", "c1", "c2"},
                         {x["comment_id"] for x in comments})
        children = [x for x in comments if x.get("parent_comment_id") == "r1"]
        self.assertEqual(2, len(children))
        self.assertEqual(2, stats["root_pages"])
        self.assertEqual(2, stats["reply_pages"])
        self.assertEqual(2, client.calls[1]["params"]["next_offset"])
        self.assertEqual(1, client.calls[3]["params"]["next_offset"])

    def test_xiaohongshu_uses_json_cursor_fields(self):
        client = FakeClient([
            {"data": {"data": {"comments": [{"id": "1", "content": "a"}],
                                "has_more": True,
                                "cursor": '{"cursor":"cursor-2","index":2,"pageArea":"ALL"}'}}},
            {"data": {"data": {"comments": [{"id": "2", "content": "b"}],
                                "has_more": False}}},
        ])
        comments, _stats = cm.fetch_all_comments(
            client, "xiaohongshu", item("xiaohongshu"),
            max_pages=10, include_replies=False)
        self.assertEqual(["1", "2"], [x["comment_id"] for x in comments])
        second = client.calls[1]["params"]
        self.assertEqual("cursor-2", second["cursor"])
        self.assertEqual(2, second["index"])
        self.assertEqual("ALL", second["pageArea"])

    def test_wechat_channels_follows_last_buffer(self):
        client = FakeClient([
            {"data": {"comments": [{"comment_id": "1", "content": "a"}],
                       "down_continue": 1, "last_buffer": "buffer-2"}},
            {"data": {"comments": [{"comment_id": "2", "content": "b"}],
                       "down_continue": 0, "last_buffer": "buffer-3"}},
        ])
        comments, _stats = cm.fetch_all_comments(
            client, "wechat_channels", item("wechat_channels"),
            max_pages=10, include_replies=False)
        self.assertEqual(["1", "2"], [x["comment_id"] for x in comments])
        self.assertEqual("buffer-2", client.calls[1]["payload"]["last_buffer"])

    def test_page_limit_is_failure_not_partial_success(self):
        client = FakeClient([
            {"data": {"comments": [{"cid": "1"}], "has_more": 1, "cursor": 20}},
        ])
        with self.assertRaisesRegex(RuntimeError, "本轮不推进评论状态"):
            cm.fetch_all_comments(
                client, "douyin", item(), max_pages=1, include_replies=False)

    def test_first_full_scan_is_baseline_then_new_comment_is_reported(self):
        first = FakeClient([
            {"data": {"comments": [{"cid": "1", "text": "old"}], "has_more": 0}},
        ])
        empty_state = {"seen_comments": {}, "content_counts": {}, "baseline_done": True}
        with mock.patch.object(cm, "load_state", return_value=empty_state):
            new_items, errors, state = cm.check_comments(
                first, [self.content()], self.args())
        self.assertEqual([], new_items)
        self.assertEqual([], errors)
        self.assertIn("douyin:content-1", state["full_scan_baselines"])

        second = FakeClient([
            {"data": {"comments": [
                {"cid": "2", "text": "new"}, {"cid": "1", "text": "old"}],
                "has_more": 0}},
        ])
        with mock.patch.object(cm, "load_state", return_value=state):
            new_items, errors, updated = cm.check_comments(
                second, [self.content()], self.args())
        self.assertEqual([], errors)
        self.assertEqual(["2"], [x["comment_id"] for x in new_items[0]["comments"]])
        self.assertEqual({"1", "2"}, set(updated["seen_comments"]["douyin:content-1"]))

    def test_incomplete_pagination_does_not_advance_content_baseline(self):
        client = FakeClient([
            {"data": {"comments": [{"cid": "1"}], "has_more": 1, "cursor": 20}},
        ])
        empty_state = {"seen_comments": {}, "content_counts": {}, "baseline_done": True}
        with mock.patch.object(cm, "load_state", return_value=empty_state):
            new_items, errors, state = cm.check_comments(
                client, [self.content()], self.args(max_pages=1))
        self.assertEqual([], new_items)
        self.assertEqual(1, len(errors))
        self.assertNotIn("douyin:content-1", state["full_scan_baselines"])
        self.assertNotIn("douyin:content-1", state["seen_comments"])

    def test_newly_discovered_content_comments_are_alerted_after_initial_baseline(self):
        client = FakeClient([
            {"data": {"comments": [{"cid": "new-1", "text": "new",
                                      "create_time": 1788220860}],
                       "has_more": 0}},
        ])
        content = self.content()
        content["newly_discovered"] = True
        state = {
            "seen_comments": {}, "content_counts": {}, "baseline_done": True,
            "monitor_started_at": "2026-09-01T00:00:00+08:00",
        }
        with mock.patch.object(cm, "load_state", return_value=state):
            new_items, errors, updated = cm.check_comments(
                client, [content], self.args())
        self.assertEqual([], errors)
        self.assertEqual("new-1", new_items[0]["comments"][0]["comment_id"])
        self.assertIn("douyin:content-1", updated["full_scan_baselines"])

    def test_new_content_historical_or_unknown_time_comments_are_only_baselined(self):
        client = FakeClient([
            {"data": {"comments": [
                {"cid": "old", "create_time": 1704067200},
                {"cid": "unknown"}], "has_more": 0}},
        ])
        content = self.content()
        content["newly_discovered"] = True
        state = {
            "seen_comments": {}, "content_counts": {}, "baseline_done": True,
            "monitor_started_at": "2026-09-01T00:00:00+08:00",
        }
        with mock.patch.object(cm, "load_state", return_value=state):
            new_items, errors, updated = cm.check_comments(
                client, [content], self.args())
        self.assertEqual([], errors)
        self.assertEqual([], new_items)
        self.assertEqual(
            {"old", "unknown"}, set(updated["seen_comments"]["douyin:content-1"]))

    def test_hourly_discovery_adds_latest_bilibili_content_only_once(self):
        client = FakeClient([{"data": {
            "item": [{"bvid": "BV-new", "title": "new", "created": 100,
                      "stat": {"reply": 3}}]
        }}])
        video_config = {"accounts": [{
            "platform": "bilibili", "account_name": "账号",
            "business_line": "业务", "platform_uid": "123",
        }]}

        def fake_load(path):
            if path == cm.VIDEO_ACCOUNTS:
                return video_config
            if path == cm.ARTICLE_ACCOUNTS:
                return {"accounts": []}
            if path == cm.VIDEO_DATA:
                return {"accounts": []}
            return {}

        existing = [{"platform": "bilibili", "content_id": "BV-old"}]
        with mock.patch.object(cm, "load_json", side_effect=fake_load):
            merged, additions, errors = cm.discover_latest_contents(client, existing)
        self.assertEqual([], errors)
        self.assertEqual(1, len(additions))
        self.assertEqual("BV-new", additions[0]["content_id"])
        self.assertTrue(additions[0]["newly_discovered"])
        self.assertEqual(2, len(merged))

    def test_state_semantics_upgrade_starts_new_monitoring_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "comment_state.json"
            state_path.write_text(json.dumps({
                "baseline_done": True,
                "seen_comments": {"bilibili:x": ["old"]},
                "content_counts": {"csdn:x": 99},
                "full_scan_baselines": ["bilibili:x"],
            }), encoding="utf-8")
            with mock.patch.object(cm, "STATE_PATH", state_path):
                state = cm.load_state()
        self.assertEqual(cm.MONITOR_STATE_VERSION, state["monitor_state_version"])
        self.assertFalse(state["baseline_done"])
        self.assertEqual({}, state["content_counts"])
        self.assertEqual([], state["full_scan_baselines"])
        self.assertEqual(["old"], state["seen_comments"]["bilibili:x"])
        self.assertTrue(state["monitor_started_at"])


if __name__ == "__main__":
    unittest.main()
