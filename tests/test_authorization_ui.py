"""Authorization UI in real Chrome with fully routed, synthetic API responses."""
import json
import os
from pathlib import Path
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'http://127.0.0.1:18798'
FIRST = 'baijiahao:测试甲'
SECOND = 'xiaohongshu:测试乙'
PUBLIC = 'csdn:公开示例'
DESKTOP = '/desktop/vnc.html?autoconnect=true&resize=scale&path=desktop/websockify'


def accounts_fixture():
    return [{'key': key, 'name': name, 'platform': platform, 'configured': False,
             'collection_mode': 'public' if public else 'authorized',
             'can_login': not public, 'can_configure': not public,
             'authorization': {'status': status}, 'health': {'ready': False}, 'job': {},
             'notification': {'mode': 'inherit', 'enabled': True, 'email': 'default@example.com',
                              'owner': '负责人', 'configured_email': '', 'configured_owner': ''}}
            for key, name, platform, public, status in (
                (FIRST, '测试甲', 'baijiahao', False, 'unconfigured'),
                (SECOND, '测试乙', 'xiaohongshu', False, 'reauth_required'),
                (PUBLIC, '公开示例', 'csdn', True, 'unconfigured'))]


def session_fixture(account=FIRST, desktop=DESKTOP, state='awaiting_login'):
    return {'account': account, 'operation_id': 'fixture-operation-1', 'state': state,
            'desktop_url': desktop, 'expires_at': '2099-01-01T12:00:00+08:00'}


class AuthorizationUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.driver = sync_playwright().start()
        cls.browser = cls.driver.chromium.launch(channel='chrome', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 1440, 'height': 1100}, service_workers='block')
        self.page = self.context.new_page()
        self.accounts = accounts_fixture()
        self.active = None
        self.posts = []
        self.errors = []
        self.foreign_requests = []
        self.desktop_requests = 0
        self.desktop = DESKTOP
        self.fail_complete = False
        self.hold_path = None
        self.held = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.context.route('**/*', self.route)

    def tearDown(self):
        self.context.close()
        self.assertEqual([], self.errors)
        self.assertEqual([], self.foreign_requests)

    def fulfill(self, route, result, status=200):
        route.fulfill(status=status, content_type='application/json', body=json.dumps(result))

    def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.netloc != '127.0.0.1:18798':
            self.foreign_requests.append(request.url)
            route.abort()
            return
        if parsed.path == '/':
            html = (ROOT / 'web/manage/index.html').read_text().replace('__CSRF_TOKEN__', 'fixture-csrf')
            route.fulfill(content_type='text/html', body=html)
            return
        if parsed.path == '/api/accounts':
            self.fulfill(route, self.accounts)
            return
        if parsed.path == '/api/login-session':
            self.fulfill(route, self.active)
            return
        if parsed.path == '/desktop/vnc.html':
            self.desktop_requests += 1
            route.fulfill(content_type='text/html', body='<html lang="zh-CN"><body style="background:#eef3f9;font:22px system-ui;padding:50px">独立授权桌面（测试页面）<p>请使用平台登录页扫码</p></body></html>')
            return
        if request.method == 'POST':
            payload = request.post_data_json
            self.posts.append((parsed.path, payload, request.headers.get('x-csrf-token')))
            if parsed.path == self.hold_path:
                self.held.append(route)
                return
            self.finish_post(route, parsed.path, payload)
            return
        route.abort()

    def finish_post(self, route, path, payload):
        if path == '/api/login':
            self.active = session_fixture(payload['account'], self.desktop)
            self.fulfill(route, {'status': 'login_opened', 'login_session': self.active})
        elif path == '/api/authorization/complete':
            if self.fail_complete:
                self.fulfill(route, {'error': '<img src=x onerror=alert(1)>账号不一致'}, 400)
            else:
                self.active['state'] = 'verifying'
                self.fulfill(route, {'status': 'verifying'})
        elif path == '/api/authorization/cancel':
            self.active = None
            self.fulfill(route, {'status': 'cancelled'})
        elif path == '/api/config/read':
            self.fulfill(route, {'provider': 'fixture'})
        elif path == '/api/notifications/save':
            account = next(row for row in self.accounts if row['key'] == payload['account'])
            mode = payload['rule']['mode']
            account['notification'] = {'mode': mode, 'enabled': mode != 'disabled',
                                       'email': payload['rule'].get('email', 'default@example.com') if mode != 'disabled' else '',
                                       'owner': payload['rule'].get('owner', ''),
                                       'configured_email': payload['rule'].get('email', ''),
                                       'configured_owner': payload['rule'].get('owner', '')}
            self.fulfill(route, {'status': 'saved'})
        else:
            self.fulfill(route, {'status': 'running'})

    def open(self):
        self.page.goto(ORIGIN)
        self.page.wait_for_function('document.querySelectorAll("#rows tr").length===3')

    def row(self, account):
        return self.page.locator(f'#rows tr[data-account="{account}"]')

    def action(self, account, kind):
        return self.row(account).locator(f'button[data-action="{kind}"]')

    def refresh(self):
        self.page.evaluate('refresh()')

    def test_verified_account_still_discloses_missing_optional_comment_metrics(self):
        self.accounts[0]['authorization']['status'] = 'authorized'
        self.accounts[0]['health'] = {'ready': True, 'optional_metric_keys': ['comment'],
                                      'metric_coverage': {'comment': 138/147}}
        self.open()
        text = self.row(FIRST).inner_text()
        self.assertIn('已授权', text)
        self.assertIn('采集验证通过', text)
        self.assertIn('覆盖 93.9%', text)
        self.assertIn('缺失保持未知', text)

    def test_public_and_authorized_accounts_have_separate_mail_settings(self):
        self.open()
        self.assertTrue(self.action(PUBLIC, 'notification').is_enabled())
        self.action(PUBLIC, 'notification').click()
        self.assertIn('公开示例', self.page.locator('#notification-title').inner_text())
        self.page.locator('#notification-mode').select_option('disabled')
        self.page.locator('#notification-save').click()
        self.page.wait_for_function('document.querySelector("#notification-editor").open===false')
        self.assertIn('已关闭', self.row(PUBLIC).inner_text())
        self.assertIn('平台默认', self.row(FIRST).inner_text())
        self.assertIn(('/api/notifications/save', {'account': PUBLIC, 'rule': {'mode': 'disabled'}},
                       'fixture-csrf'), self.posts)

    def test_authorization_and_data_acceptance_are_separate_and_public_needs_no_login(self):
        self.accounts[0]['authorization']['status'] = 'authorized'
        self.accounts[0]['health'] = {'ready': False, 'identity_verified': True}
        self.open()
        first = self.row(FIRST)
        self.assertIn('已授权', first.inner_text())
        self.assertIn('数据覆盖待验证', first.inner_text())
        self.assertEqual('重新授权', self.action(FIRST, 'login').inner_text())
        self.assertIn('需要重新授权', self.row(SECOND).inner_text())
        self.assertIn('无需登录', self.row(PUBLIC).inner_text())
        self.assertTrue(self.action(PUBLIC, 'login').is_disabled())
        self.assertTrue(self.action(PUBLIC, 'edit').is_disabled())
        self.assertTrue(self.action(PUBLIC, 'probe').is_enabled())
        self.assertIn('1 个已授权 · 0 个采集验证通过', self.page.locator('#summary').inner_text())
        self.page.evaluate('(key)=>action(key,"login")', PUBLIC)
        self.assertEqual([], self.posts)

    def test_desktop_login_complete_and_saved_authorization_transition(self):
        self.open()
        self.action(FIRST, 'login').click()
        self.page.wait_for_function('document.querySelector("#authorization-desktop").getAttribute("src")!==null')
        self.assertIn('测试甲 · 百家号', self.page.locator('#authorization-target').inner_text())
        self.assertIn('2099', self.page.locator('#authorization-expiry').inner_text())
        self.assertTrue(self.action(SECOND, 'login').is_disabled())
        self.assertTrue(self.action(SECOND, 'probe').is_enabled())
        self.assertIn('测试甲', self.row(SECOND).inner_text())
        self.page.locator('#authorization-complete').click()
        self.page.wait_for_function('document.querySelector("#authorization-state").textContent.includes("正在验证")')
        self.assertTrue(self.page.locator('#authorization-complete').is_disabled())
        self.assertTrue(self.page.locator('#authorization-cancel').is_disabled())
        self.assertEqual(('/api/authorization/complete', {'account': FIRST, 'operation_id': 'fixture-operation-1'}, 'fixture-csrf'), self.posts[-1])
        self.active = None
        self.accounts[0]['authorization'] = {'status': 'authorized'}
        self.refresh()
        self.assertFalse(self.page.locator('#authorization').is_visible())
        self.assertIsNone(self.page.locator('#authorization-desktop').get_attribute('src'))
        self.assertIn('已授权', self.row(FIRST).inner_text())
        self.assertIn('尚未通过验证', self.row(FIRST).inner_text())
        self.assertTrue(self.action(SECOND, 'login').is_enabled())

    def test_pending_login_and_complete_cannot_repeat_or_switch_account(self):
        self.open()
        self.hold_path = '/api/login'
        self.action(FIRST, 'login').click()
        self.page.wait_for_function('document.querySelectorAll("#rows button[data-action=login]:disabled").length===3')
        self.page.evaluate('([first,second])=>{action(first,"login");action(second,"login")}', [FIRST, SECOND])
        self.assertEqual(1, len(self.posts))
        self.page.evaluate('(key)=>action(key,"probe")', SECOND)
        self.page.evaluate('(key)=>action(key,"login")', SECOND)
        self.assertEqual(['/api/login', '/api/probe'], [path for path, _, _ in self.posts])
        self.assertTrue(self.action(SECOND, 'login').is_disabled())
        self.hold_path = None
        self.finish_post(self.held.pop(), '/api/login', {'account': FIRST})
        self.page.wait_for_selector('#authorization:visible')
        self.hold_path = '/api/authorization/complete'
        self.page.locator('#authorization-complete').click()
        self.page.evaluate('()=>{finishAuthorization(false);finishAuthorization(true)}')
        self.assertEqual(1, sum(path == '/api/authorization/complete' for path, _, _ in self.posts))
        self.assertEqual(0, sum(path == '/api/authorization/cancel' for path, _, _ in self.posts))
        self.hold_path = None
        self.finish_post(self.held.pop(), '/api/authorization/complete', {'account': FIRST})
        self.page.wait_for_function('document.querySelector("#authorization-state").textContent.includes("正在验证")')

    def test_refresh_restores_existing_session_without_reloading_desktop_and_cancel_releases_it(self):
        self.active = session_fixture()
        self.open()
        self.page.wait_for_selector('#authorization:visible')
        self.page.wait_for_function('document.querySelector("#authorization-desktop").contentDocument?.body.textContent.includes("测试页面")')
        initial = self.desktop_requests
        self.refresh()
        self.assertEqual(initial, self.desktop_requests)
        self.page.locator('#authorization-cancel').click()
        self.page.wait_for_selector('#authorization', state='hidden')
        self.assertEqual(('/api/authorization/cancel', {'account': FIRST, 'operation_id': 'fixture-operation-1'}, 'fixture-csrf'), self.posts[-1])
        self.assertTrue(self.action(SECOND, 'login').is_enabled())
        self.assertIsNone(self.page.locator('#authorization-desktop').get_attribute('src'))

    def test_local_chrome_and_xiaohongshu_two_page_guidance(self):
        self.active = session_fixture(SECOND, None)
        self.open()
        self.assertTrue(self.page.locator('#local-desktop').is_visible())
        self.assertFalse(self.page.locator('#desktop-area').is_visible())
        self.assertIn('创作后台和小红书主站', self.page.locator('#authorization-guide').inner_text())
        self.assertTrue(self.page.locator('#authorization-complete').is_enabled())

    def test_errors_are_text_and_keep_same_operation_for_retry(self):
        self.active = session_fixture()
        self.fail_complete = True
        self.open()
        self.page.locator('#authorization-complete').click()
        self.page.wait_for_function('document.querySelector("#notice").textContent.includes("账号不一致")')
        self.assertEqual(0, self.page.locator('#notice img').count())
        self.assertTrue(self.page.locator('#authorization-complete').is_enabled())
        self.assertTrue(self.action(SECOND, 'login').is_disabled())
        self.fail_complete = False
        self.page.locator('#authorization-complete').click()
        self.page.wait_for_function('document.querySelector("#authorization-state").textContent.includes("正在验证")')
        self.assertEqual([{'account': FIRST, 'operation_id': 'fixture-operation-1'}] * 2,
                         [body for path, body, _ in self.posts if path == '/api/authorization/complete'])

    def test_expired_session_does_not_submit_and_external_desktop_is_never_loaded(self):
        self.active = session_fixture(desktop='https://example.org/desktop/vnc.html')
        self.active['expires_at'] = '2000-01-01T00:00:00Z'
        self.open()
        self.assertTrue(self.page.locator('#authorization-complete').is_disabled())
        self.assertTrue(self.page.locator('#authorization-cancel').is_enabled())
        self.assertIn('超时', self.page.locator('#authorization-state').inner_text())
        self.assertIsNone(self.page.locator('#authorization-desktop').get_attribute('src'))
        self.page.evaluate('finishAuthorization(false)')
        self.assertEqual([], self.posts)

    def test_narrow_layout_keeps_authorization_actions_accessible(self):
        self.active = session_fixture()
        self.open()
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.assertTrue(self.page.locator('#authorization-complete').is_visible())
        self.assertEqual(True, self.page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
        if os.environ.get('PROMOTION_AUTH_QA_DIR'):
            output = Path(os.environ['PROMOTION_AUTH_QA_DIR'])
            output.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(output / 'authorization-narrow.png'), full_page=True)
            self.page.set_viewport_size({'width': 1440, 'height': 1100})
            self.page.screenshot(path=str(output / 'authorization-desktop.png'), full_page=True)


if __name__ == '__main__':
    unittest.main()
