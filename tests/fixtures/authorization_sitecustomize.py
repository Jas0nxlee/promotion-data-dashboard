"""Test-only sitecustomize, explicitly mounted by smoke_authorization_flow.py.

This fixture uses real BrowserSource sessions and synthetic routed content. It
must never be added to the production PYTHONPATH or copied into production images.
"""
import json
import os
from pathlib import Path

if os.environ.get('PROMOTION_SIMULATED_PLATFORM') != '1':
    raise RuntimeError('Synthetic authorization fixture requires its isolated test environment')

import provider_setup
from providers.base import Collection, ProviderError, now
from providers.browser import BrowserSource
from providers.registry import ProviderRegistry

KEY = 'baijiahao:国科安芯'
FIXTURE_ORIGIN = 'https://baijiahao.baidu.com'
FIXTURE_URL = FIXTURE_ORIGIN + '/fixture/login'
COOKIE = 'promotion_fixture_identity'
CONTROL = Path('/fixture-control')
provider_setup.ENTRIES['baijiahao'] = 'about:blank'


def install_routes(context, identity):
    """No request from this browser context is allowed to reach a platform."""
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
    <title>模拟平台授权 · 仅测试</title><body style="font:22px system-ui;padding:60px;background:#eef3f9">
    <h1>模拟百家号授权</h1><p>这是隔离测试页面，不连接真实平台。</p>
    <button id="fixture-login" style="font:inherit;padding:15px">模拟登录</button>
    <p id="fixture-status">尚未模拟登录</p><script>
    document.querySelector('#fixture-login').onclick=()=>{
      document.cookie=COOKIE_NAME+'='+encodeURIComponent(IDENTITY)+'; Path=/; Secure; SameSite=Lax';
      document.querySelector('#fixture-status').textContent='模拟登录完成';
    };</script></body></html>'''.replace('COOKIE_NAME', json.dumps(COOKIE)).replace('IDENTITY', json.dumps(str(identity)))
    context.route('**/*', lambda route: route.abort())
    context.route(FIXTURE_ORIGIN + '/fixture**', lambda route: route.fulfill(content_type='text/html', body=document))


class FixtureProvider:
    def __init__(self, account, settings):
        self.account = account
        self.settings = settings
        self.browser = BrowserSource(account, settings)

    @property
    def call_count(self):
        return self.browser.call_count

    def _profile(self):
        CONTROL.mkdir(parents=True, exist_ok=True)
        count_file = CONTROL / 'profile-calls'
        count_file.write_text(str(int(count_file.read_text()) + 1 if count_file.exists() else 1))
        if (CONTROL / 'expire').exists():
            raise ProviderError('session_expired', 'simulated_platform: fixture session expired')
        expected = str(self.account['platform_uid'])
        context = self.browser.context
        install_routes(context, expected)
        page = context.new_page()
        try:
            page.goto(FIXTURE_ORIGIN + '/fixture/profile', wait_until='domcontentloaded')
            cookies = context.cookies(FIXTURE_ORIGIN)
            identity = next((cookie['value'] for cookie in cookies if cookie['name'] == COOKIE), '')
            if identity != expected:
                raise ProviderError('identity_mismatch', 'simulated_platform: click the fixture login button first')
            self.browser.call_count += 1
            return {'verified_account_id': identity, 'nickname': self.account['account_name'], 'total': 1,
                    'simulated_platform': True}
        finally:
            page.close()

    def collect(self, max_pages=200, **kwargs):
        with self.browser.session():
            profile = self._profile()
            return Collection(profile, [{'article_id': 'fixture-article-1',
                'title': '明确模拟的授权生命周期测试文章', 'url': FIXTURE_ORIGIN + '/fixture/article',
                'published_at': now(), 'stats': {'read': 7, 'like': 1, 'comment': 0, 'share': None, 'collect': None},
                'simulated_platform': True}], source='baijiahao_creator', request_count=self.call_count)


_original_get = ProviderRegistry.get


def fixture_get(self, account):
    key = account['platform'] + ':' + account['account_name']
    if key != KEY:
        return _original_get(self, account)
    if key not in self.providers:
        canonical = provider_setup.accounts(include_public=True)[key]
        settings = self.config.get('accounts', {}).get(key)
        if not settings:
            raise ProviderError('setup_required', 'simulated_platform: fixture needs authorization')
        self.providers[key] = FixtureProvider(canonical, settings)
    return self.providers[key]


ProviderRegistry.get = fixture_get
