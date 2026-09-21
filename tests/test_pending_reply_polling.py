import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import comment_monitor as cm
import comment_timeline as timeline_store


class PendingReplyPollingTests(unittest.TestCase):
    def run_scan(self, *, replied=False, count=0, limit=0, no_replies=False):
        now = datetime.fromisoformat('2026-09-21T09:00:00+08:00')
        content = {'platform': 'bilibili', 'platform_label': 'B站', 'account_key': 'bilibili:test',
                   'account_name': 'test', 'content_id': 'old', 'published_at': '2026-08-07T18:00:00+08:00'}
        root = {'comment_id': 'root', 'created_at': '2026-09-18T19:00:00+08:00',
                'content': 'klipper?', 'user_ids': ['visitor'], 'reply_count': count}
        reply = {'comment_id': 'answer', 'parent_comment_id': 'root', 'created_at': '2026-09-20T14:33:00+08:00',
                 'content': '支持klipper', 'user_ids': ['official']}
        timeline = {'timeline_started_at': '2026-09-01T00:00:00+08:00', 'events': {}}
        timeline_store.record_comment(timeline, content, root, observed_at=now.isoformat())
        if replied:
            timeline_store.record_official_reply(timeline, content, root, reply, observed_at=now.isoformat())
        state = {'baseline_done': True, 'seen_comments': {'bilibili:old': ['root']},
                 'full_scan_baselines': ['bilibili:old'],
                 'content_poll_at': {'bilibili:old': now.isoformat()},
                 'root_reply_counts': {'bilibili:old:root': count}}
        args = SimpleNamespace(platform=[], limit=limit, no_replies=no_replies, tiered_polling=True, max_pages=200)
        newer = {**content, 'content_id': 'newer', 'published_at': '2026-09-21T00:00:00+08:00'}
        state['content_poll_at']['bilibili:newer'] = now.isoformat()
        with patch.object(cm, 'fetch_root_comments', return_value=([root], 1)) as roots, \
             patch.object(cm, 'fetch_comment_replies', return_value=([reply], 1)), \
             patch.object(cm, 'refresh_verified_official_identity'):
            new_items, errors, _ = cm.check_comments(object(), [newer, content], args, state=state,
                timeline=timeline, official_identities={'bilibili:test': {'ids': {'official'}}}, now=now)
        return timeline, roots.call_count, new_items, errors

    def test_pending_old_content_rechecked_even_with_zero_reply_counter(self):
        timeline, calls, notifications, errors = self.run_scan()
        self.assertEqual([], errors)
        self.assertEqual([], notifications)
        self.assertEqual(1, calls)
        self.assertIn('bilibili:answer', timeline['events'])

    def test_pending_thread_rechecked_when_reply_count_has_not_grown(self):
        timeline, calls, _, errors = self.run_scan(count=1)
        self.assertEqual([], errors)
        self.assertIn('bilibili:answer', timeline['events'])

    def test_pending_content_is_not_displaced_by_recent_content_limit(self):
        timeline, calls, _, errors = self.run_scan(limit=1)
        self.assertEqual([], errors)
        self.assertIn('bilibili:answer', timeline['events'])

    def test_confirmed_reply_returns_content_to_tiered_schedule(self):
        _, calls, _, errors = self.run_scan(replied=True)
        self.assertEqual([], errors)
        self.assertEqual(0, calls)

    def test_explicit_no_replies_keeps_original_polling_behavior(self):
        _, calls, _, errors = self.run_scan(no_replies=True)
        self.assertEqual([], errors)
        self.assertEqual(0, calls)
