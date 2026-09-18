"""Video-channel keepalive is read-only, account-scoped, and skips stale logins."""

import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import session_maintenance
from providers.base import ProviderError


class FakeProvider:
    def __init__(self, identity):
        self.settings = {'provider': 'wechat_channels_creator'}
        self.entered = 0
        self.exported = 0
        self.identity = identity
        self.browser = self

    @contextmanager
    def session(self):
        self.entered += 1
        yield
        self.exported += 1

    def _profile(self):
        return {'verified_account_id': self.identity}


class SessionMaintenanceTests(unittest.TestCase):
    def test_only_idle_authorized_video_account_is_refreshed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            catalog = {f'wechat_channels:{name}': {'platform': 'wechat_channels',
                       'account_name': name, 'platform_uid': name} for name in ('甲', '乙', '丙')}
            catalog['bilibili:视频'] = {'platform': 'bilibili', 'account_name': '视频'}
            first = root / (hashlib.sha256('wechat_channels:甲'.encode()).hexdigest()[:24] + '.storage.json')
            second = root / (hashlib.sha256('wechat_channels:乙'.encode()).hexdigest()[:24] + '.storage.json')
            first.write_text('{}'); second.write_text('{}')
            import os
            os.utime(first, (100, 100)); os.utime(second, (9000, 9000))
            provider = FakeProvider('甲')
            registry = SimpleNamespace(get=lambda account: provider)
            authorization = SimpleNamespace(read=lambda key: {'status': 'reauth_required' if key.endswith('丙') else 'authorized'})
            report = session_maintenance.maintain_once(
                idle_seconds=3600, clock=lambda: 10000, catalog=catalog, registry=registry,
                authorization=authorization, sessions=root, state_path=root / 'state.json')
            self.assertEqual('refreshed', report['accounts']['wechat_channels:甲']['status'])
            self.assertEqual('recent_session', report['accounts']['wechat_channels:乙']['status'])
            self.assertEqual('waiting_for_authorization', report['accounts']['wechat_channels:丙']['status'])
            self.assertEqual(1, provider.entered)
            self.assertEqual(1, provider.exported)
            self.assertNotIn('bilibili:视频', report['accounts'])
            self.assertEqual(report, json.loads((root / 'state.json').read_text()))

    def test_wrong_identity_cannot_export(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            provider = FakeProvider('另一个账号')
            key = 'wechat_channels:甲'
            result = session_maintenance.maintain_once(
                idle_seconds=3600, clock=lambda: 10000,
                catalog={key: {'platform': 'wechat_channels', 'account_name': '甲', 'platform_uid': '甲'}},
                registry=SimpleNamespace(get=lambda _account: provider),
                authorization=SimpleNamespace(read=lambda _key: {'status': 'authorized'}),
                sessions=root, state_path=root / 'state.json')
            self.assertEqual('identity_mismatch', result['accounts'][key]['reason'])
            self.assertEqual(0, provider.exported)


if __name__ == '__main__':
    unittest.main()
