"""Authorization HTTP boundaries over real loopback sockets, with no real login process."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import control_panel
from login_manager import LoginManager
from providers.authorization import AuthorizationStore
from providers.base import ProviderError
from providers.settings import SettingsStore
from comment_notifications import NotificationStore

FIRST = 'baijiahao:fixture-first'
SECOND = 'xiaohongshu:fixture-second'
EXTERNAL = 'https://dashboard.example.test:9443'
OPERATION = 'a' * 32


class AuthorizationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='promotion-auth-http-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SettingsStore(self.root / 'providers.json')
        self.notification_store = NotificationStore(self.root / 'comment_notification_settings.json')
        self.store.update(FIRST, {'provider': 'baijiahao_creator'})
        self.auth = AuthorizationStore(self.root / 'sessions' / 'authorization')
        self.catalog = {key: {'platform': key.split(':')[0], 'account_name': key.split(':')[1],
                             'platform_uid': 'canonical'} for key in (FIRST, SECOND)}
        self.executor = Mock()
        patches = [patch.dict(os.environ, {'PROMOTION_PANEL_ORIGIN': EXTERNAL, 'PROMOTION_LOGIN_MODE': '',
                                          'PROMOTION_PANEL_ALLOWED_ORIGINS': 'http://10.0.1.13:18762,http://localhost:18762'}),
                   patch.object(control_panel, 'SettingsStore', return_value=self.store),
                   patch.object(control_panel, 'NotificationStore', return_value=self.notification_store),
                   patch.object(control_panel, 'AuthorizationStore', return_value=self.auth),
                   patch.object(control_panel, 'ThreadPoolExecutor', return_value=self.executor),
                   patch.object(control_panel, 'accounts', return_value=self.catalog),
                   patch.object(control_panel, 'read_verification', return_value={'ready': False}),
                   patch.object(control_panel.subprocess, 'Popen', side_effect=AssertionError('unexpected process launch')),
                   patch.object(control_panel.subprocess, 'run', side_effect=AssertionError('unexpected browser launch'))]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.panel = control_panel.Panel()
        self.addCleanup(self.panel.close)
        self.view = SimpleNamespace(lock=threading.RLock(), _sweep=Mock(), active={
            'account': FIRST, 'platform': 'baijiahao', 'operation_id': OPERATION,
            'state': 'awaiting_login', 'expires_at': '2099-01-01T00:00:00Z',
            'message': 'fixture awaiting login', 'desktop_url': '/desktop/vnc.html',
            'process': object(), 'worker': object(), 'port': 62001,
            'folder': Path('/private/fixture/profile'), 'settings': {'private': 'not-for-ui'},
            'original': {'private': 'not-for-ui'}, 'profile': '/private/fixture/profile'})
        self.manager = Mock()
        self.manager.status.side_effect = lambda: LoginManager.status(self.view)
        self.manager.start.side_effect = lambda key: LoginManager.status(self.view)
        def operation(account, operation_id, action):
            if account != FIRST or operation_id != OPERATION:
                raise ProviderError('authorization_conflict', 'fixture mismatched account or operation')
            return {'status': action}
        self.manager.complete.side_effect = lambda key, op: operation(key, op, 'verifying')
        self.manager.cancel.side_effect = lambda key, op: operation(key, op, 'cancelled')
        self.panel.login_manager = self.manager
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), control_panel.handler(self.panel))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, body=None, *, headers=None, method='POST'):
        required = {'Host': 'dashboard.example.test:9443'}
        if method == 'POST':
            required.update({'Origin': EXTERNAL, 'X-CSRF-Token': self.panel.token, 'Content-Type': 'application/json'})
        if headers:
            required.update(headers)
        required = {key: value for key, value in required.items() if value is not None}
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=required)
            response = connection.getresponse()
            payload = json.loads(response.read())
            return response.status, payload, dict(response.getheaders())
        finally:
            connection.close()

    def test_configured_proxy_host_origin_and_csrf_route_authorization_actions(self):
        code, body, headers = self.request('/api/login', {'account': FIRST})
        self.assertEqual(200, code)
        self.assertEqual(OPERATION, body['login_session']['operation_id'])
        self.assertEqual('no-store', headers['Cache-Control'])
        self.manager.start.assert_called_once_with(FIRST)
        for action, expected in (('complete', 'verifying'), ('cancel', 'cancelled')):
            with self.subTest(action=action):
                code, body, _ = self.request('/api/authorization/' + action, {'account': FIRST, 'operation_id': OPERATION})
                self.assertEqual(200, code)
                self.assertEqual(expected, body['status'])
                getattr(self.manager, action).assert_called_once_with(FIRST, OPERATION)

    def test_lan_alias_requires_matching_origin_and_csrf(self):
        for origin, host in [('http://10.0.1.13:18762', '10.0.1.13:18762'),
                             ('http://localhost:18762', 'localhost:18762')]:
            code, _, _ = self.request('/api/login', {'account': FIRST}, headers={'Origin': origin, 'Host': host})
            self.assertEqual(200, code)
        self.manager.start.reset_mock()
        for headers in [{'Origin': 'http://localhost:18762', 'Host': '10.0.1.13:18762'},
                        {'Origin': 'http://10.0.1.13:18762', 'Host': '10.0.1.13:18762', 'X-CSRF-Token': None},
                        {'Origin': 'http://10.0.1.99:18762', 'Host': '10.0.1.99:18762'}]:
            code, _, _ = self.request('/api/login', {'account': FIRST}, headers=headers)
            self.assertEqual(403, code)
        self.manager.start.assert_not_called()

    def test_invalid_origin_aliases_fail_closed(self):
        for value in ['http://*', 'http://localhost:99999', 'http://user:pass@localhost',
                      'http://localhost/path', 'http://localhost/']:
            with patch.dict(os.environ, {'PROMOTION_PANEL_ALLOWED_ORIGINS': value}), self.assertRaises(ValueError):
                control_panel.configured_origins()

    def test_missing_csrf_untrusted_origin_and_wrong_host_are_rejected_before_manager(self):
        invalid = ({'X-CSRF-Token': None}, {'X-CSRF-Token': 'wrong'}, {'Origin': None},
                   {'Origin': 'https://evil.example.test'}, {'Origin': EXTERNAL + '.evil'},
                   {'Host': 'evil.example.test'}, {'Host': 'dashboard.example.test:9443.evil'})
        for path in ('/api/login', '/api/authorization/complete', '/api/authorization/cancel'):
            for header in invalid:
                with self.subTest(path=path, header=header):
                    code, body, _ = self.request(path, {'account': FIRST, 'operation_id': OPERATION}, headers=header)
                    self.assertEqual(403, code)
                    self.assertIn('error', body)
        self.assertEqual([], self.manager.mock_calls)
        self.executor.submit.assert_not_called()

    def test_login_session_get_uses_production_status_whitelist_without_private_process_state(self):
        code, body, _ = self.request('/api/login-session', method='GET')
        self.assertEqual(200, code)
        self.assertEqual({'account', 'platform', 'operation_id', 'state', 'expires_at', 'message', 'desktop_url'}, set(body))
        encoded = json.dumps(body)
        for forbidden in ('process', 'profile', 'worker', '62001', 'folder', 'settings', 'original', 'not-for-ui'):
            self.assertNotIn(forbidden, encoded)
        code, body, _ = self.request('/api/login-session', method='GET', headers={'Host': 'evil.example.test'})
        self.assertEqual(403, code)
        self.manager.status.assert_called_once_with()

    def test_authorizing_account_cannot_save_config_or_start_normal_probe(self):
        self.auth.begin(FIRST)
        before = self.store.path.read_bytes()
        for path, payload in (('/api/config/save', {'account': FIRST, 'settings': {'provider': 'browser'}}),
                              ('/api/probe', {'account': FIRST})):
            with self.subTest(path=path):
                code, body, _ = self.request(path, payload)
                self.assertEqual(400, code)
                self.assertIn('authorization_in_progress', body['error'])
        self.assertEqual(before, self.store.path.read_bytes())
        self.executor.submit.assert_not_called()
        self.assertEqual([], self.manager.mock_calls)

    def test_unknown_catalog_account_is_rejected_without_calling_manager(self):
        for path in ('/api/login', '/api/authorization/complete', '/api/authorization/cancel'):
            code, body, _ = self.request(path, {'account': 'baijiahao:unknown', 'operation_id': OPERATION})
            self.assertEqual(400, code)
            self.assertIn('unknown_account', body['error'])
        self.assertEqual([], self.manager.mock_calls)

    def test_account_notification_setting_is_isolated_and_requires_csrf(self):
        path = '/api/notifications/save'
        code, body, _ = self.request(path, {'account': FIRST, 'rule': {
            'mode': 'enabled', 'email': 'first@example.com', 'owner': '甲'}},
            headers={'X-CSRF-Token': None})
        self.assertEqual(403, code)
        self.assertFalse(self.notification_store.path.exists())
        code, body, _ = self.request(path, {'account': FIRST, 'rule': {
            'mode': 'enabled', 'email': 'first@example.com', 'owner': '甲'}})
        self.assertEqual(200, code)
        self.assertEqual('first@example.com', body['rule']['email'])
        self.assertEqual({'mode': 'enabled', 'email': 'first@example.com', 'owner': '甲'},
                         self.notification_store.read()[FIRST])
        code, body, _ = self.request('/api/accounts', method='GET')
        self.assertEqual(200, code)
        self.assertEqual('first@example.com', next(row for row in body if row['key'] == FIRST)['notification']['email'])
        self.assertEqual('inherit', next(row for row in body if row['key'] == SECOND)['notification']['mode'])
        code, body, _ = self.request(path, {'account': FIRST, 'rule': {'mode': 'enabled', 'email': 'bad'}})
        self.assertEqual(400, code)
        self.assertIn('invalid_notification', body['error'])
        self.assertEqual('first@example.com', self.notification_store.read()[FIRST]['email'])
        code, _, _ = self.request(path, {'account': 'bilibili:missing', 'rule': {'mode': 'disabled'}})
        self.assertEqual(400, code)

    def test_valid_catalog_wrong_account_or_operation_still_reaches_manager_conflict_guard(self):
        for action in ('complete', 'cancel'):
            for key, operation in ((SECOND, OPERATION), (FIRST, 'wrong'), (FIRST, None)):
                with self.subTest(action=action, key=key, operation=operation):
                    code, body, _ = self.request('/api/authorization/' + action, {'account': key, 'operation_id': operation})
                    self.assertEqual(400, code)
                    self.assertIn('authorization_conflict', body['error'])
                    getattr(self.manager, action).assert_called_with(key, operation)

    def test_local_mode_without_login_manager_reports_explicit_unsupported_completion(self):
        self.panel.login_manager = None
        for action in ('complete', 'cancel'):
            code, body, _ = self.request('/api/authorization/' + action, {'account': FIRST, 'operation_id': OPERATION})
            self.assertEqual(400, code)
            self.assertIn('unsupported', body['error'])
            self.assertIn('本机模式', body['error'])
        code, body, _ = self.request('/api/login-session', method='GET')
        self.assertEqual(200, code)
        self.assertIsNone(body)
        self.assertEqual([], self.manager.mock_calls)
