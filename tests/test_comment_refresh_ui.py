import copy
import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


class CommentRefreshUITests(unittest.TestCase):
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
        self.context = self.browser.new_context(service_workers='block')
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.fail = False
        self.hold = False
        self.held = []
        self.requests = 0
        self.root = {'event_type': 'comment', 'platform': 'bilibili', 'account_key': 'bilibili:test',
                     'account_name': 'test', 'content_id': 'video', 'comment_id': 'root',
                     'content': 'klipper?', 'content_title': 'fixture', 'platform_created_at': '2026-09-18T19:00:00+08:00',
                     'last_seen_at': '2026-09-20T11:19:00+08:00'}
        self.snapshot = {'events': [self.root], 'updated_at': '2026-09-20T11:19:00+08:00',
                         'timeline_started_at': '2026-09-01T00:00:00+08:00'}
        self.context.add_init_script('''
          const originalInterval = window.setInterval;
          window.setInterval = (callback, delay, ...args) => {
            if (delay === 60000) { window.__refreshTick = callback; return 12345; }
            return originalInterval(callback, delay, ...args);
          };
        ''')
        self.context.route('**/*', self.route)
        self.page.goto('http://fixture.test/comments/')
        self.page.wait_for_function('typeof window.__refreshTick === "function"')

    def tearDown(self):
        self.context.close()
        self.assertEqual([], self.errors)

    def route(self, route):
        path = urlsplit(route.request.url).path
        if path.endswith('/comment_timeline.json'):
            self.requests += 1
            if self.hold:
                self.held.append(route)
                return
            route.fulfill(status=503 if self.fail else 200, content_type='application/json', body=json.dumps(self.snapshot))
            return
        target = ROOT / 'web' / path.lstrip('/')
        if path.endswith('/'):
            target = target / 'index.html'
        if target.is_file():
            content_type = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css'}.get(target.suffix, 'application/octet-stream')
            route.fulfill(content_type=content_type, body=target.read_bytes())
        else:
            route.fulfill(status=404, body='')

    def test_timer_refresh_updates_replies_and_preserves_filters(self):
        self.page.select_option('#fPlatform', 'bilibili')
        self.page.select_option('#fAccount', 'bilibili:test')
        self.page.locator('#fSearch').fill('klipper')
        self.page.select_option('#fStatus', 'pending')
        self.assertIn('尚未检测到官方回复', self.page.locator('#timelineList').inner_text())
        self.snapshot = copy.deepcopy(self.snapshot)
        self.snapshot['updated_at'] = '2026-09-21T10:00:00+08:00'
        self.snapshot['events'].append({**self.root, 'event_type': 'official_reply', 'comment_id': 'reply',
                                       'parent_comment_id': 'root', 'content': '支持klipper', 'author': 'test'})
        self.page.evaluate('window.__refreshTick()')
        self.assertEqual('bilibili', self.page.input_value('#fPlatform'))
        self.assertEqual('bilibili:test', self.page.input_value('#fAccount'))
        self.assertEqual('pending', self.page.input_value('#fStatus'))
        self.assertEqual('klipper', self.page.input_value('#fSearch'))
        self.assertEqual(0, self.page.locator('.thread-card').count())
        self.page.select_option('#fStatus', 'replied')
        self.assertIn('支持klipper', self.page.locator('#timelineList').inner_text())
        self.assertIn('已官方回复', self.page.locator('.reply-status').inner_text())

    def test_failed_refresh_retains_snapshot_and_recovers(self):
        before = self.page.locator('#updatedAt').inner_text()
        self.fail = True
        self.page.evaluate('window.__refreshTick()')
        self.assertEqual(before, self.page.locator('#updatedAt').inner_text())
        self.assertIn('klipper?', self.page.locator('#timelineList').inner_text())
        self.assertIn('保留上次数据', self.page.locator('#refreshStatus').inner_text())
        self.fail = False
        self.page.evaluate('window.__refreshTick()')
        self.assertIn('每 60 秒', self.page.locator('#refreshStatus').inner_text())

    def test_slow_refresh_does_not_overlap_requests(self):
        self.hold = True
        before = self.requests
        self.page.evaluate('void window.__refreshTick()')
        self.page.wait_for_timeout(100)
        self.page.evaluate('window.__refreshTick()')
        self.assertEqual(before + 1, self.requests)
        self.held.pop().fulfill(content_type='application/json', body=json.dumps(self.snapshot))
        self.page.wait_for_function('refreshing === false')
