"""A live comment scan can persist discoveries without touching the mail queue."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import comment_monitor as cm


class NoNotificationsTests(unittest.TestCase):
    def scan(self, *, existing_queue, targeted=False):
        with tempfile.TemporaryDirectory(prefix="promotion-comment-no-mail-") as tmp:
            directory = Path(tmp)
            state_path = directory / "comment_state.json"
            queue_path = directory / "comment_alert.json"
            timeline_path = directory / "comment_timeline.json"
            public_path = directory / "public_timeline.json"
            state_path.write_text(json.dumps({
                "monitor_state_version": cm.MONITOR_STATE_VERSION,
                "baseline_done": True,
                "monitor_started_at": "2026-09-15T09:00:00+08:00",
                "seen_comments": {"bilibili:BV-fixture": []},
                "full_scan_baselines": ["bilibili:BV-fixture"],
            }), encoding="utf-8")
            timeline_path.write_text(json.dumps({
                "version": 1,
                "timeline_started_at": "2026-09-15T09:00:00+08:00",
                "events": {},
            }), encoding="utf-8")
            if existing_queue:
                queue_path.write_bytes(b'{"emails":[{"id":"pending"}],"generated_at":"old"}\n')
            old_queue = queue_path.read_bytes() if queue_path.exists() else None

            item = {
                "account_key": "bilibili:fixture", "platform": "bilibili",
                "platform_label": "B站", "account_name": "fixture",
                "content_id": "BV-fixture", "title": "测试作品",
            }
            ignored = {**item, "content_id": "BV-ignored", "title": "未选作品"}
            root = {
                "comment_id": "root", "user": "访客", "user_ids": ["guest"],
                "content": "提问", "reply_count": 1,
                "created_at": "2026-09-15T10:00:00+08:00",
            }
            reply = {
                "comment_id": "reply", "parent_comment_id": "root",
                "user": "官方", "user_ids": ["official"], "content": "答复",
                "created_at": "2026-09-15T10:05:00+08:00",
            }
            client = SimpleNamespace(call_count=0, usage_snapshot=lambda: {"used": 2, "limit": 0})
            with patch.object(cm, "STATE_PATH", state_path), \
                 patch.object(cm, "ALERT_PATH", queue_path), \
                 patch.object(cm.timeline_store, "TIMELINE_PATH", timeline_path), \
                 patch.object(cm.timeline_store, "WEB_TIMELINE_PATH", public_path), \
                 patch.object(cm, "load_dotenv"), \
                 patch.object(cm, "build_content_list", return_value=[item, ignored] if targeted else [item]), \
                 patch("providers.history.retire_replaced_comment_contents", side_effect=lambda items, *_: items), \
                 patch.object(cm, "ProviderRegistry", return_value=client), \
                 patch.object(cm, "discovery_is_due", return_value=False), \
                 patch.object(cm, "load_official_identities", return_value={}), \
                 patch.object(cm, "refresh_verified_official_identity"), \
                 patch.object(cm, "fetch_root_comments", return_value=([root], 1)), \
                 patch.object(cm, "fetch_comment_replies", return_value=([reply], 1)), \
                 patch.object(cm, "is_official_author", side_effect=lambda comment, *_: comment["comment_id"] == "reply"), \
                 patch.object(cm, "write_alerts", side_effect=AssertionError("mail queue must not be written")), \
                 patch.object(sys, "argv", ["comment_monitor.py", "--no-notifications", "--no-discovery",
                                            *(["--platform", "bilibili:fixture", "--content-id", "BV-fixture"] if targeted else [])]):
                cm.main()

            state = json.loads(state_path.read_text(encoding="utf-8"))
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
            self.assertEqual(["root"], state["seen_comments"]["bilibili:BV-fixture"])
            if targeted:
                self.assertNotIn("bilibili:BV-ignored", state["seen_comments"])
            self.assertEqual(1, state["last_scan"]["official_replies_added"])
            self.assertTrue(state["last_scan"]["complete"])
            self.assertIn("bilibili:root", timeline["events"])
            self.assertIn("bilibili:reply", timeline["events"])
            self.assertTrue(public_path.exists())
            self.assertEqual(old_queue, queue_path.read_bytes() if queue_path.exists() else None)

    def test_new_comments_and_replies_do_not_create_mail_queue(self):
        self.scan(existing_queue=False)

    def test_existing_pending_mail_queue_is_unchanged(self):
        self.scan(existing_queue=True)

    def test_targeted_retry_scans_only_selected_id_without_alerts(self):
        self.scan(existing_queue=False, targeted=True)


if __name__ == "__main__":
    unittest.main()
