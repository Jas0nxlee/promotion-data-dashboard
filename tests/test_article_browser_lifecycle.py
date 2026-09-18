"""A failed or completed Toutiao browser must release its loop before Sohu starts."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import fetch_article_data as articles


class BrowserLifecycleTests(unittest.TestCase):
    def collect(self, *, fail_toutiao):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accounts = [
                {'platform': platform, 'collector': platform, 'account_name': name,
                 'platform_uid': uid, 'business_line': '测试', 'content_type': '图文'}
                for platform, name, uid in [('toutiao', '头条测试', '11'), ('sohu', '搜狐测试', '22')]
            ]
            config = root / 'accounts.json'
            config.write_text(json.dumps({'accounts': accounts}, ensure_ascii=False))
            state = {'driver_open': False, 'closes': 0}

            def toutiao_collect(_self, account):
                state['driver_open'] = True
                if fail_toutiao:
                    raise RuntimeError('fixture failed after browser startup')
                return {**articles.base_account(account), 'verified_account_id': '11'}, []

            def close(_self):
                state['driver_open'] = False
                state['closes'] += 1

            def sohu_collect(_self, account):
                if state['driver_open']:
                    raise RuntimeError('Playwright Sync API inside asyncio loop')
                return {**articles.base_account(account), 'verified_account_id': '22'}, []

            args = SimpleNamespace(out=str(root / 'snapshot.json'), debug=False,
                public_interval=0, interval=0, max_pages=1, manual_input=str(root / 'manual.json'),
                toutiao_pages=1, toutiao_timeout=5, toutiao_headed=False,
                wechat_pages=1, only=[])
            with patch.object(articles, 'CONFIG_PATH', config), \
                 patch.object(articles, 'load_previous', return_value={'accounts': [], 'articles': []}), \
                 patch.object(articles, 'record_public_article_verification'), \
                 patch.object(articles.ToutiaoCollector, 'collect', toutiao_collect), \
                 patch.object(articles.ToutiaoCollector, 'close', close), \
                 patch.object(articles.SohuCollector, 'collect', sohu_collect):
                result = articles.collect(args)
            self.assertFalse(state['driver_open'])
            self.assertGreaterEqual(state['closes'], 2)
            by_key = {row['account_key']: row for row in result['accounts']}
            self.assertEqual('ok', by_key['sohu:搜狐测试']['status'])
            self.assertEqual('error' if fail_toutiao else 'ok', by_key['toutiao:头条测试']['status'])

    def test_success_releases_browser_before_next_platform(self):
        self.collect(fail_toutiao=False)

    def test_failure_releases_browser_before_next_platform(self):
        self.collect(fail_toutiao=True)


if __name__ == '__main__':
    unittest.main()
