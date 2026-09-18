"""Account mail choices must not affect another account or bypass a later opt-out."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import comment_monitor
import send_comment_alerts
from comment_notifications import NotificationStore


def comment(account, comment_id):
    return {'platform': 'bilibili', 'platform_label': 'B站', 'account_key': f'bilibili:{account}',
            'account_name': account, 'title': '测试作品', 'content_id': comment_id,
            'comments': [{'comment_id': comment_id, 'content': '测试评论', 'user': '用户'}]}


class AccountNotificationTests(unittest.TestCase):
    def test_two_accounts_on_same_platform_have_independent_routes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = NotificationStore(root / 'settings.json')
            store.update('bilibili:甲', {'mode': 'enabled', 'email': 'first@example.com', 'owner': '甲'})
            queue = root / 'queue.json'
            with patch.object(comment_monitor, 'NotificationStore', return_value=store), \
                    patch.object(comment_monitor, 'ALERT_PATH', queue):
                emails, _ = comment_monitor.write_alerts([comment('甲', '1'), comment('乙', '2')])
            self.assertEqual(2, len(emails))
            self.assertEqual('first@example.com', next(row for row in emails if row['account_key'] == 'bilibili:甲')['to'])
            self.assertEqual('wanghuo@ucas.com.cn', next(row for row in emails if row['account_key'] == 'bilibili:乙')['to'])
            self.assertEqual({'bilibili:甲', 'bilibili:乙'}, {row['account_key'] for row in emails})

    def test_disabling_one_account_removes_its_pending_mail_before_smtp(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = NotificationStore(root / 'settings.json')
            queue = root / 'queue.json'
            with patch.object(comment_monitor, 'NotificationStore', return_value=store), \
                    patch.object(comment_monitor, 'ALERT_PATH', queue):
                comment_monitor.write_alerts([comment('甲', '1'), comment('乙', '2')])
            store.update('bilibili:甲', {'mode': 'disabled'})
            sent = []

            class FakeSmtp:
                def __enter__(self): return self
                def __exit__(self, *_args): return False
                def send_message(self, message): sent.append(message)

            with patch.object(send_comment_alerts, 'NotificationStore', return_value=store), \
                    patch.object(send_comment_alerts, 'connect', return_value=FakeSmtp()):
                self.assertEqual(1, send_comment_alerts.send_pending(queue, {'from_addr': 'from@example.com'}))
            self.assertEqual(1, len(sent))
            self.assertNotIn('甲', sent[0].get_content())
            self.assertIn('乙', sent[0].get_content())
            self.assertFalse(queue.exists())

    def test_changing_recipient_retargets_queued_mail_and_is_stable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = NotificationStore(root / 'settings.json')
            queue = root / 'queue.json'
            with patch.object(comment_monitor, 'NotificationStore', return_value=store), \
                    patch.object(comment_monitor, 'ALERT_PATH', queue):
                comment_monitor.write_alerts([comment('甲', '1')])
            store.update('bilibili:甲', {'mode': 'enabled', 'email': 'updated@example.com', 'owner': '新负责人'})
            payload = json.loads(queue.read_text())
            self.assertTrue(send_comment_alerts.reconcile_pending(payload, store.read()))
            self.assertEqual('updated@example.com', payload['emails'][0]['to'])
            self.assertFalse(send_comment_alerts.reconcile_pending(payload, store.read()))

    def test_returning_to_platform_default_retargets_last_custom_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = NotificationStore(root / 'settings.json')
            store.update('bilibili:甲', {'mode': 'enabled', 'email': 'old@example.com', 'owner': ''})
            queue = root / 'queue.json'
            with patch.object(comment_monitor, 'NotificationStore', return_value=store), \
                    patch.object(comment_monitor, 'ALERT_PATH', queue):
                comment_monitor.write_alerts([comment('甲', '1')])
            store.update('bilibili:甲', {'mode': 'inherit'})
            payload = json.loads(queue.read_text())
            self.assertTrue(send_comment_alerts.reconcile_pending(payload, store.read()))
            self.assertEqual('wanghuo@ucas.com.cn', payload['emails'][0]['to'])


if __name__ == '__main__':
    unittest.main()
