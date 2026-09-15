import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import comment_timeline as timeline


CN_TZ = timezone(timedelta(hours=8))


class CommentTimelineTests(unittest.TestCase):
    @staticmethod
    def item():
        return {
            "platform": "bilibili", "platform_label": "B站",
            "account_key": "bilibili:official", "account_name": "official",
            "business_line": "业务", "content_id": "BV1", "title": "作品",
            "url": "https://example.com/BV1",
        }

    def test_only_records_events_after_timeline_start(self):
        store = {
            "version": 1,
            "timeline_started_at": "2026-09-15T10:00:00+08:00",
            "events": {},
        }
        old = {"comment_id": "old", "created_at": "2026-09-15T09:59:59+08:00"}
        new = {
            "comment_id": "new", "created_at": "2026-09-15T10:01:00+08:00",
            "user": "用户", "content": "新评论", "reply_count": 0,
        }
        self.assertFalse(timeline.record_comment(
            store, self.item(), old, observed_at="2026-09-15T10:02:00+08:00"))
        self.assertTrue(timeline.record_comment(
            store, self.item(), new, observed_at="2026-09-15T10:02:00+08:00"))
        self.assertEqual(["bilibili:new"], list(store["events"]))

    def test_official_reply_links_to_new_comment_and_computes_response(self):
        store = {
            "version": 1,
            "timeline_started_at": "2026-09-15T10:00:00+08:00",
            "events": {},
        }
        root = {
            "comment_id": "root", "created_at": "2026-09-15T10:10:00+08:00",
            "user": "用户", "content": "问题", "reply_count": 1,
        }
        reply = {
            "comment_id": "reply", "created_at": "2026-09-15T10:40:00+08:00",
            "user": "official", "content": "官方答复",
        }
        timeline.record_comment(
            store, self.item(), root, observed_at="2026-09-15T10:20:00+08:00")
        self.assertTrue(timeline.record_official_reply(
            store, self.item(), root, reply, observed_at="2026-09-15T10:41:00+08:00"))
        snapshot = timeline.build_public_snapshot(store)
        self.assertEqual(1, snapshot["stats"]["comments"])
        self.assertEqual(1, snapshot["stats"]["official_replies"])
        self.assertEqual(1800, snapshot["stats"]["response_median_seconds"])
        self.assertEqual(1, snapshot["stats"]["replied_comments"])

    def test_reply_to_prelaunch_comment_is_not_recorded(self):
        store = {
            "version": 1,
            "timeline_started_at": "2026-09-15T10:00:00+08:00",
            "events": {},
        }
        root = {"comment_id": "old-root", "created_at": "2026-09-14T10:00:00+08:00"}
        reply = {"comment_id": "new-reply", "created_at": "2026-09-15T11:00:00+08:00"}
        self.assertFalse(timeline.record_official_reply(
            store, self.item(), root, reply, observed_at="2026-09-15T11:01:00+08:00"))
        self.assertEqual({}, store["events"])

    def test_save_writes_private_and_public_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp) / "private.json"
            public = Path(tmp) / "public.json"
            now = datetime(2026, 9, 15, 12, tzinfo=CN_TZ)
            store = timeline.load_timeline(private, now=now)
            snapshot = timeline.save_timeline(
                store, api_usage={"used": 12, "limit": 800},
                private_path=private, public_path=public, now=now)
            self.assertTrue(private.exists())
            self.assertTrue(public.exists())
            self.assertEqual(12, snapshot["api_usage"]["used"])
            self.assertEqual("2026-09-15T12:00:00+08:00", snapshot["timeline_started_at"])


if __name__ == "__main__":
    unittest.main()
