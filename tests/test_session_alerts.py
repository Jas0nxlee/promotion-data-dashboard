import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))

import session_alerts
from providers.authorization import AuthorizationStore


KEY = 'bilibili:测试账号'
RECIPIENT = 'linwei.li@ucas.com.cn'


class SessionAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = AuthorizationStore(self.root / 'authorization')
        self.path = self.root / 'alerts.json'
        self.catalog = {KEY: {'platform': 'bilibili', 'account_name': '测试账号'}}
        self.sender = Mock()
        self.mode = patch.object(session_alerts, 'is_test', return_value=False)
        self.mode.start()
        self.addCleanup(self.mode.stop)
        self.authorize()

    def authorize(self):
        state = self.store.begin(KEY)
        self.store.complete(KEY, state['operation_id'])

    def expire(self):
        self.store.require_reauthorization(KEY, 'session_expired', 'private-cookie-must-not-leak')

    def run_cycle(self):
        return session_alerts.run_once(recipient=RECIPIENT, state_path=self.path,
                                      authorization=self.store, catalog=self.catalog,
                                      sender=self.sender)

    def test_expiry_sends_once_across_repeated_failures_and_restart(self):
        self.expire()
        self.assertEqual(1, self.run_cycle()['sent'])
        self.expire()
        self.assertEqual(0, self.run_cycle()['sent'])
        item = self.sender.call_args.args[0]
        self.assertEqual(RECIPIENT, item['to'])
        self.assertIn('测试账号', item['body'])
        self.assertIn('18762', item['body'])
        self.assertNotIn('private-cookie', item['body'])
        self.sender.assert_called_once()

    def test_smtp_failure_stays_pending_until_retry_succeeds(self):
        self.expire()
        self.sender.side_effect = OSError('SMTP unavailable')
        self.assertEqual(1, self.run_cycle()['failed'])
        pending = json.loads(self.path.read_text())['accounts'][KEY]
        self.assertFalse(pending.get('sent_at'))
        self.sender.side_effect = None
        self.assertEqual(1, self.run_cycle()['sent'])
        self.assertEqual(0, self.run_cycle()['sent'])

    def test_shared_smtp_failure_does_not_retry_login_for_every_account(self):
        self.expire()
        second = 'douyin:测试账号二'
        self.catalog[second] = {'platform': 'douyin', 'account_name': '测试账号二'}
        state = self.store.begin(second)
        self.store.complete(second, state['operation_id'])
        self.store.require_reauthorization(second, 'session_expired')
        self.sender.side_effect = OSError('SMTP unavailable')
        result = self.run_cycle()
        self.assertEqual(2, result['pending'])
        self.sender.assert_called_once()

    def test_recovery_discards_pending_and_next_expiry_can_alert(self):
        self.expire()
        self.sender.side_effect = OSError()
        self.run_cycle()
        self.authorize()
        self.sender.side_effect = None
        self.assertEqual(0, self.run_cycle()['sent'])
        self.assertNotIn(KEY, json.loads(self.path.read_text())['accounts'])
        self.expire()
        self.assertEqual(1, self.run_cycle()['sent'])

    def test_recovery_between_polls_starts_new_incident(self):
        self.expire()
        self.run_cycle()
        self.authorize()
        self.expire()
        self.assertEqual(1, self.run_cycle()['sent'])

    def test_network_failure_and_initial_authorization_cancel_do_not_alert(self):
        self.store.require_reauthorization(KEY, 'browser_error', 'network failure')
        self.assertEqual(0, self.run_cycle()['sent'])
        state = self.store.begin(KEY)
        self.store.cancel(KEY, state['operation_id'])
        self.assertEqual(0, self.run_cycle()['sent'])
        self.sender.assert_not_called()

    def test_manual_authorization_suspends_pending_mail(self):
        self.expire()
        self.sender.side_effect = OSError()
        self.run_cycle()
        self.store.begin(KEY)
        self.sender.reset_mock()
        self.assertEqual(0, self.run_cycle()['sent'])
        self.sender.assert_not_called()

    def test_failed_reauthorization_resumes_existing_pending_alert(self):
        self.expire()
        self.sender.side_effect = OSError()
        self.run_cycle()
        state = self.store.begin(KEY)
        self.store.cancel(KEY, state['operation_id'])
        self.sender.side_effect = None
        self.assertEqual(1, self.run_cycle()['sent'])

    def test_invalid_authorization_state_preserves_pending_without_sending(self):
        self.expire()
        self.sender.side_effect = OSError()
        self.run_cycle()
        self.store._path(KEY).write_text('{broken')
        self.sender.reset_mock()
        self.assertEqual(1, self.run_cycle()['failed'])
        self.sender.assert_not_called()
        self.assertFalse(json.loads(self.path.read_text())['accounts'][KEY]['sent_at'])

    def test_invalid_or_disabled_recipient_cannot_send(self):
        self.expire()
        with patch.dict('os.environ', {'SESSION_ALERT_RECIPIENT': 'disabled'}):
            self.assertEqual(0, session_alerts.run_once()['sent'])
        with self.assertRaises(RuntimeError):
            session_alerts.run_once(recipient='a@example.com\r\nBcc: b@example.com')
        self.sender.assert_not_called()

    def test_test_mode_queues_without_smtp(self):
        self.expire()
        with patch.object(session_alerts, 'is_test', return_value=True):
            result = self.run_cycle()
        self.assertEqual(0, result['sent'])
        self.assertEqual(1, result['pending'])
        self.sender.assert_not_called()

    def test_corrupt_dedupe_state_fails_closed(self):
        self.expire()
        self.path.write_text('{broken')
        with self.assertRaises(RuntimeError):
            self.run_cycle()
        self.sender.assert_not_called()

    def test_smtp_uses_dedicated_recipient_without_comment_routing(self):
        self.expire()
        self.run_cycle()
        item = self.sender.call_args.args[0]
        messages = []

        class FakeSmtp:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def send_message(self, message): messages.append(message); return {}

        with patch.object(session_alerts, 'smtp_config', return_value=({'from_addr': 'from@example.com'}, [])), \
                patch.object(session_alerts, 'connect', return_value=FakeSmtp()), \
                patch('send_comment_alerts.reconcile_pending', side_effect=AssertionError('comment routing')):
            session_alerts.send_alert(item)
        self.assertEqual(RECIPIENT, messages[0]['To'])
        self.assertIn(item['id'], messages[0]['Message-ID'])


if __name__ == '__main__':
    unittest.main()
