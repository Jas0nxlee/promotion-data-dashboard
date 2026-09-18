import copy
import json
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from unittest.mock import Mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
from providers.base import ProviderError
from providers.browser import BrowserSource
from providers.douyin import DouyinProvider, video_record, comment_record, COMMENTS, REPLIES

ACCOUNT = {'platform': 'douyin', 'account_name': '测试', 'platform_uid': 'test_handle'}
UID = '2298429093459075'
CID = '7483043075963571475'
R1, R2 = '7514917147157381947', '7483447296530105127'
C1, C2, C3 = '7516462264343380736', '7516834834607751995', '7516834834607751996'


def video(cid=CID):
    raw = {'aweme_id': cid, 'author_user_id': UID, 'duration': 90500, 'create_time': 1742281910,
           'item_id': int(cid) + 123,  # Unsafe rounded legacy field must never supply identity.
           'statistics': {'aweme_id': cid, 'play_count': 32, 'digg_count': 2, 'comment_count': 1, 'share_count': 0, 'collect_count': 3}}
    item = {'id': cid, 'metrics': {'view_count': '32', 'like_count': '2', 'comment_count': '1', 'share_count': '0', 'favorite_count': '3'}, 'review': {'status': 5}}
    return raw, item


def work_page(cid=CID, more=False, total=1, cursor=5):
    raw, item = video(cid)
    return {'status_code': 0, 'aweme_list': [raw], 'items': [item], 'has_more': more, 'total': total, 'max_cursor': cursor}


class Source:
    def __init__(self, pages):
        self.pages = copy.deepcopy(pages)
        self.call_count = 0
        self.calls = []

    @contextmanager
    def session(self):
        yield self

    def get_json(self, url, params):
        self.call_count += 1
        self.calls.append((url, params))
        return self.pages.pop(0)


def profile():
    return {'status_code': 0, 'user': {'unique_id': ACCOUNT['platform_uid'], 'uid': UID, 'nickname': '测试', 'follower_count': 321}}


class DouyinTests(unittest.TestCase):
    def test_full_catalog_cursor_and_exact_id_ignore_rounded_legacy_field(self):
        source = Source([profile(), work_page(more=True, total=2), work_page(R1, total=2)])
        result = DouyinProvider(ACCOUNT, {}, source).collect()
        self.assertTrue(result.complete)
        self.assertEqual([CID, R1], [r['video_id'] for r in result.records])
        self.assertEqual(5, source.calls[-1][1]['max_cursor'])
        self.assertEqual(5, result.records[0]['review_status'])
        self.assertEqual(32, result.records[0]['stats']['play'])

    def test_wrong_login_and_wrong_video_author_fail(self):
        p = profile(); p['user']['unique_id'] = 'other'
        with self.assertRaisesRegex(ProviderError, 'identity_mismatch'):
            DouyinProvider(ACCOUNT, {}, Source([p])).collect()
        raw, item = video();raw['author_user_id'] = 'other'
        with self.assertRaisesRegex(ProviderError, 'identity_mismatch'):
            video_record(raw, item, UID)

    def test_metric_disagreement_and_wrong_join_rejected(self):
        raw, item = video();item['metrics']['view_count'] = '33'
        with self.assertRaisesRegex(ProviderError, '不一致'):
            video_record(raw, item, UID)
        item['id'] = R1
        with self.assertRaisesRegex(ProviderError, 'identity_mismatch'):
            video_record(raw, item, UID)

    def test_blocked_success_code_and_total_change_fail_closed(self):
        with self.assertRaisesRegex(ProviderError, 'platform_error'):
            DouyinProvider(ACCOUNT, {}, Source([{'status_code': 0, 'status_msg': 'blocked'}])).collect()
        source = Source([profile(), work_page(more=True, total=2), work_page(R1, total=3)])
        with self.assertRaisesRegex(ProviderError, 'incomplete_pagination'):
            DouyinProvider(ACCOUNT, {}, source).collect()

    def test_discovery_is_partial_and_repeated_cursor_is_error(self):
        source = Source([profile(), work_page(more=True, total=2)])
        self.assertFalse(DouyinProvider(ACCOUNT, {}, source).collect(discovery=True).complete)
        source = Source([profile(), work_page(more=True, total=3), work_page(R1, more=True, total=3)])
        with self.assertRaisesRegex(ProviderError, 'incomplete_pagination'):
            DouyinProvider(ACCOUNT, {}, source).collect()

    def test_reply_preserves_root_target_and_author_id(self):
        r = comment_record(comment(C2, R1, target=C1), CID, R1)
        self.assertEqual(R1, r['parent_comment_id'])
        self.assertEqual(C1, r['reply_to_comment_id'])
        self.assertEqual([UID], r['user_ids'])
        with self.assertRaisesRegex(ProviderError, 'identity_mismatch'):
            comment_record(comment(C1, R1), CID, R2)

    def test_evicted_cdp_body_repeated_once_with_live_get_and_no_redirect(self):
        response = Mock(url='https://www.douyin.com' + COMMENTS, status=200)
        response.request = SimpleNamespace(method='GET', headers={'referer': 'https://www.douyin.com/video/' + CID})
        response.body.side_effect = RuntimeError('Network.getResponseBody: No resource with given identifier found')
        retry = Mock(status=200)
        retry.body.return_value = json.dumps(envelope(None, total=0)).encode()
        source = SimpleNamespace(context=SimpleNamespace(request=Mock()), budget=Mock(), call_count=1)
        source.context.request.get.return_value = retry
        provider = DouyinProvider(ACCOUNT, {}, source)
        self.assertEqual(0, provider._comment_payload(response)['total'])
        source.context.request.get.assert_called_once_with(response.url, headers=response.request.headers, timeout=20000, max_redirects=0)
        retry.dispose.assert_called_once()
        self.assertEqual(2, source.call_count)

    def test_cdp_fallback_does_not_retry_denial_mutation_or_unrelated_host(self):
        for url, status, method in [('https://www.douyin.com' + COMMENTS, 403, 'GET'),
                                    ('https://www.douyin.com' + COMMENTS, 200, 'POST'),
                                    ('https://example.com' + COMMENTS, 200, 'GET')]:
            with self.subTest(url=url, status=status, method=method):
                response = Mock(url=url, status=status)
                response.request = SimpleNamespace(method=method, headers={})
                response.body.side_effect = RuntimeError('No resource with given identifier')
                source = SimpleNamespace(context=SimpleNamespace(request=Mock()), budget=Mock(), call_count=0)
                with self.assertRaises(RuntimeError):
                    DouyinProvider(ACCOUNT, {}, source)._comment_payload(response)
                source.context.request.get.assert_not_called()

    def test_initial_hydration_detachment_reacquires_comment_section(self):
        page = Mock()
        page.evaluate.side_effect = [False, True]
        DouyinProvider._reach_comment_section(page, 1000)
        self.assertEqual(2, page.evaluate.call_count)
        page.wait_for_timeout.assert_called_once_with(250)
        page.evaluate.reset_mock();page.evaluate.side_effect = RuntimeError('Navigation failed')
        with self.assertRaisesRegex(RuntimeError, 'Navigation failed'):
            DouyinProvider._reach_comment_section(page, 1000)
        self.assertEqual(1, page.evaluate.call_count)


def comment(cid, parent='0', target='0', replies=0):
    return {'cid': cid, 'aweme_id': CID, 'reply_id': parent, 'reply_to_reply_id': target,
            'reply_comment_total': replies, 'text': '示例', 'user': {'uid': UID, 'nickname': '作者'},
            'create_time': 1749702999, 'digg_count': 0, 'reply_comment': []}


def envelope(rows, total=5, more=0, cursor=0):
    return {'status_code': 0, 'comments': rows, 'total': total, 'has_more': more, 'cursor': cursor}


class DouyinBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.driver = sync_playwright().start()
        cls.browser = cls.driver.chromium.launch(channel='chrome', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.driver.stop()

    def setUp(self):
        self.context = self.browser.new_context(service_workers='block')
        self.settings = {'timeout_ms': 2000}
        self.source = BrowserSource(ACCOUNT, self.settings);self.source.context = self.context
        self.provider = DouyinProvider(ACCOUNT, self.settings, self.source)
        self.roots = [envelope([comment(R1, replies=3)], more=1, cursor=1), envelope([comment(R2)])]
        self.children = [envelope([comment(C1, R1)], total=3, more=1, cursor=1),
                         envelope([comment(C2, R1, target=C1), comment(C3, R1)], total=3)]
        self.query_cid = CID
        self.requests = []
        self.mirror_first = False
        def route(route):
            parsed = urlsplit(route.request.url)
            if parsed.path.startswith('/video/'):
                route.fulfill(content_type='text/html', body=f'''<meta charset="utf-8"><div data-e2e="comment-list">
                <div data-e2e="comment-item"><span>根评论</span><button class="comment-reply-expand-btn" onclick="sub()"><span>展开回复</span></button></div>
                <div style="height:1000px"></div><div data-e2e="comment-item">第二条</div></div><div style="height:1000px"></div>
                <script>let rp=0,sp=0;async function root(){{let p=++rp;let u='https://www-hj.douyin.com{COMMENTS}?aweme_id={self.query_cid}&p='+p+'&cursor='+(p-1);await fetch(u);if(p===1&&{str(self.mirror_first).lower()})await fetch(u)}};
                window.onscroll=()=>{{if(rp===1)root()}};
                async function sub(){{await fetch('https://www-hj.douyin.com{REPLIES}?item_id={self.query_cid}&comment_id={R1}&p='+(++sp))}};root();</script>''')
            elif parsed.path in (COMMENTS, REPLIES):
                self.requests.append(parsed.path)
                rows = self.roots if parsed.path == COMMENTS else self.children
                index = int(parse_qs(parsed.query)['p'][0]) - 1
                route.fulfill(content_type='application/json', headers={'Access-Control-Allow-Origin': '*'}, body=json.dumps(rows[min(index, len(rows)-1)]))
            else:
                route.abort()
        self.context.route('**/*', route)

    def tearDown(self):
        self.context.close()

    def scan(self, limit=10, replies=True, root=None):
        return self.provider._scan_comments(CID, limit, replies, root)

    def test_root_and_reply_pagination_and_cached_threads(self):
        rows, stats = self.scan()
        self.assertEqual(5, len(rows));self.assertEqual(2, stats['root_pages']);self.assertEqual(2, stats['reply_pages'])
        self.assertEqual(3, len(self.provider._replies[(CID, R1, 10)]))
        self.assertEqual(0, len(self.context.pages))

    def test_root_only_does_not_request_replies(self):
        rows, stats = self.scan(replies=False)
        self.assertEqual(2, len(rows));self.assertNotIn(REPLIES, self.requests)

    def test_mirrored_same_cursor_response_is_not_a_second_page(self):
        self.mirror_first = True
        rows, stats = self.scan()
        self.assertEqual(5, len(rows))
        self.assertEqual(2, stats['root_pages'])
        self.assertEqual(3, self.requests.count(COMMENTS))

    def test_targeted_replies_do_not_require_remaining_root_pages(self):
        rows, stats = self.scan(root=R1)
        self.assertEqual(1, stats['root_pages']);self.assertEqual(3, sum(bool(r['parent_comment_id']) for r in rows))

    def test_zero_terminal_page_is_valid_but_blocked_success_is_not(self):
        self.roots = [envelope([], total=0)]
        self.assertEqual([], self.scan()[0])
        self.roots = [{'status_code': 0, 'status_msg': 'blocked'}]
        with self.assertRaisesRegex(ProviderError, 'platform_error'):
            self.scan()

    def test_null_comments_only_valid_with_explicit_zero_and_end(self):
        self.roots = [envelope(None, total=0)]
        self.assertEqual([], self.scan()[0])
        self.roots[0]['has_more'] = 1
        with self.assertRaisesRegex(ProviderError, 'schema_changed'):
            self.scan()
        self.roots[0]['has_more'] = 0
        self.roots[0].pop('comments')
        with self.assertRaisesRegex(ProviderError, 'schema_changed'):
            self.scan()

    def test_wrong_content_missing_marker_and_limit_fail_closed(self):
        self.query_cid = R2
        with self.assertRaisesRegex(ProviderError, 'identity_mismatch'):
            self.scan()
        self.query_cid = CID
        with self.assertRaisesRegex(ProviderError, 'incomplete_pagination'):
            self.scan(limit=1)
        self.roots[0].pop('has_more')
        with self.assertRaisesRegex(ProviderError, 'schema_changed'):
            self.scan()

    def test_reply_total_and_parent_must_match(self):
        self.children[0]['comments'][0]['reply_id'] = R2
        with self.assertRaisesRegex(ProviderError, 'identity_mismatch'):
            self.scan()
        self.children[0]['comments'][0]['reply_id'] = R1
        self.children[0]['total'] = 2
        with self.assertRaisesRegex(ProviderError, 'incomplete_pagination'):
            self.scan()

    def test_root_total_mismatch_and_reply_repeat_fail_closed(self):
        self.roots[0]['total'] = self.roots[1]['total'] = 6
        with self.assertRaisesRegex(ProviderError, 'coverage_limited'):
            self.scan()
        self.roots[0]['total'] = self.roots[1]['total'] = 5
        self.children[1] = copy.deepcopy(self.children[0])
        with self.assertRaisesRegex(ProviderError, 'incomplete_pagination'):
            self.scan()
