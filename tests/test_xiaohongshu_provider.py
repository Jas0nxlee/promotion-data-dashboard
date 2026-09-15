import copy
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest
import json
from urllib.parse import urlsplit, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from providers.base import Pages, ProviderError
from providers.xiaohongshu import XiaohongshuProvider, note_record, comment_record, COMMENTS, REPLIES
from providers.browser import BrowserSource

ACCOUNT = {"platform": "xiaohongshu", "account_name": "测试", "provided_id": "123456789", "platform_uid": "a" * 24}
NOTE = {"id": "b" * 24, "display_title": "示例笔记", "time": "2026-09-15 13:16",
        "likes": 2, "view_count": 100, "collected_count": 4, "comments_count": 3, "shared_count": 1,
        "xsec_token": "private-navigation-value", "type": "normal", "tab_status": 1}


class Source:
    call_count = 2

    def __init__(self):
        self.profile = {"red_num": "123456789", "name": "测试", "fans_count": 8}
        self.result = Pages([copy.deepcopy(NOTE)], 1, True, envelopes=[{
            "data": {"tags": [{"checked": True, "notes_count": 1}]}}])

    @contextmanager
    def session(self):
        yield self

    def pages(self, name, **kwargs):
        return Pages([self.profile], 1, True) if name == "profile" else self.result


class XiaohongshuTests(unittest.TestCase):
    def test_lifetime_fields_and_navigation_tokens_do_not_leak(self):
        source = Source()
        result = XiaohongshuProvider(ACCOUNT, {}, source).collect()
        self.assertTrue(result.complete)
        row = result.records[0]
        self.assertEqual({"read": 100, "like": 2, "comment": 3, "share": 1, "collect": 4}, row["stats"])
        self.assertEqual("2026-09-15T13:16:00+08:00", row["published_at"])
        self.assertNotIn("private-navigation-value", str(result))

    def test_account_number_mismatch_stops_collection(self):
        source = Source()
        source.profile["red_num"] = "different"
        with self.assertRaisesRegex(ProviderError, "identity_mismatch"):
            XiaohongshuProvider(ACCOUNT, {}, source).collect()

    def test_pinned_duplicate_deduplicated_and_total_only_on_first_page(self):
        source = Source()
        source.result.rows.append(copy.deepcopy(NOTE))
        source.result.envelopes.append({"data": {"tags": []}})
        result = XiaohongshuProvider(ACCOUNT, {}, source).collect()
        self.assertTrue(result.complete)
        self.assertEqual(1, len(result.records))

    def test_total_changes_or_count_mismatch_cannot_claim_complete(self):
        source = Source()
        source.result.envelopes.append({"data": {"tags": [{"checked": True, "notes_count": 2}]}})
        with self.assertRaisesRegex(ProviderError, "incomplete_pagination"):
            XiaohongshuProvider(ACCOUNT, {}, source).collect()
        source.result.envelopes.pop(0)
        self.assertFalse(XiaohongshuProvider(ACCOUNT, {}, source).collect().complete)

    def test_invalid_id_rejected_and_negative_metrics_unknown(self):
        with self.assertRaisesRegex(ProviderError, "稳定"):
            note_record({**NOTE, "id": ""})
        self.assertIsNone(note_record({**NOTE, "view_count": -1})["stats"]["read"])

    def test_comment_identity_and_reply_target(self):
        row = comment_record(comment("c", parent="d"), NOTE["id"], "d" * 24)
        self.assertEqual("d" * 24, row["reply_to_comment_id"])
        self.assertEqual([ACCOUNT["platform_uid"]], row["user_ids"])
        self.assertNotIn("private-token", str(row))
        with self.assertRaisesRegex(ProviderError, "identity_mismatch"):
            comment_record(comment("c"), "e" * 24)

    def test_public_login_is_required_and_must_match_creator(self):
        source = Source()
        source.result = Pages([{"guest": True}], 1, True)
        provider = XiaohongshuProvider(ACCOUNT, {}, source)
        with self.assertRaisesRegex(ProviderError, "session_expired"):
            provider._public_profile()
        source.result.rows = [{"guest": False, "user_id": "other", "red_id": ACCOUNT["provided_id"]}]
        with self.assertRaisesRegex(ProviderError, "identity_mismatch"):
            provider._public_profile()


def comment(char, parent=None):
    value = {"id": char * 24, "note_id": NOTE["id"], "content": "回复示例", "like_count": "2",
             "user_info": {"user_id": ACCOUNT["platform_uid"], "nickname": "作者", "xsec_token": "private-token"},
             "create_time": 1789106612000, "sub_comments": [], "sub_comment_count": "0",
             "sub_comment_has_more": False, "sub_comment_cursor": ""}
    if parent:
        value["target_comment"] = {"id": parent * 24}
    return value


def envelope(rows, more=False, cursor=""):
    return {"code": 0, "success": True, "data": {"user_id": ACCOUNT["platform_uid"],
            "comments": rows, "has_more": more, "cursor": cursor}}


class XiaohongshuBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.driver = sync_playwright().start()
        cls.browser = cls.driver.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        self.context = self.browser.new_context(service_workers="block")
        self.settings = {"timeout_ms": 1500}
        self.source = BrowserSource(ACCOUNT, self.settings)
        self.source.context = self.context
        self.provider = XiaohongshuProvider(ACCOUNT, self.settings, self.source)
        root = comment("c")
        root.update(sub_comments=[comment("d", "c")], sub_comment_count="3",
                    sub_comment_has_more=True, sub_comment_cursor="d" * 24)
        self.roots = [envelope([root], True, "c" * 24), envelope([comment("f")])]
        self.children = [envelope([comment("e", "d")], True, "e" * 24), envelope([comment("a", "c")])]
        self.total = 5
        self.empty_html = ""
        self.query_note = NOTE["id"]
        self.requests = []

        def route(route):
            parsed = urlsplit(route.request.url)
            if parsed.hostname == "www.xiaohongshu.com":
                route.fulfill(content_type="text/html", body=f'''<meta charset="utf-8"><div class="note-scroller" style="height:150px;overflow:auto" onscroll="next()">
                 {self.empty_html}<div class="total">共 {self.total} 条评论</div><div class="parent-comment">
                 <div id="comment-{'c'*24}">一级评论</div><div class="reply-container"><button class="show-more" onclick="sub()">展开回复</button></div></div>
                 <div style="height:1000px"></div></div><script>
                 let rp=0,sp=0;
                 async function roots(){{await fetch('https://edith.xiaohongshu.com{COMMENTS}?note_id={self.query_note}&p='+(++rp))}}
                 function next(){{if(rp===1) roots()}}
                 async function sub(){{await fetch('https://edith.xiaohongshu.com{REPLIES}?note_id={self.query_note}&root_comment_id={'c'*24}&p='+(++sp))}}
                 roots();</script>''')
            elif parsed.path in (COMMENTS, REPLIES):
                self.requests.append(parsed.path)
                pages = self.roots if parsed.path == COMMENTS else self.children
                index = int(parse_qs(parsed.query)["p"][0]) - 1
                route.fulfill(content_type="application/json", headers={"Access-Control-Allow-Origin": "*"},
                              body=json.dumps(pages[min(index, len(pages)-1)]))
            else:
                route.abort()
        self.context.route("**/*", route)

    def tearDown(self):
        self.context.close()

    def scan(self, limit=10, replies=True, root=None):
        return self.provider._scan_comments("https://www.xiaohongshu.com/fixture", NOTE["id"], limit, replies, root)

    def test_two_root_pages_and_two_reply_pages_preserve_relationships(self):
        rows, stats = self.scan()
        self.assertEqual(5, len(rows))
        self.assertEqual(2, stats["root_pages"])
        self.assertEqual(2, stats["reply_pages"])
        reply = next(x for x in rows if x["comment_id"] == "e" * 24)
        self.assertEqual("c" * 24, reply["parent_comment_id"])
        self.assertEqual("d" * 24, reply["reply_to_comment_id"])
        self.assertEqual(0, len(self.context.pages))

    def test_root_only_scan_does_not_expand_children(self):
        rows, stats = self.scan(replies=False)
        self.assertEqual(2, len(rows))
        self.assertEqual(0, stats["reply_pages"])
        self.assertNotIn(REPLIES, self.requests)

    def test_zero_comments_requires_authenticated_terminal_and_empty_dom(self):
        self.roots = [envelope([])]
        self.empty_html = '<div class="no-comments">这是一片荒地点击评论</div>'
        rows, stats = self.scan()
        self.assertEqual([], rows)
        self.assertEqual(1, stats["root_pages"])
        self.roots[0]["data"]["has_more"] = True
        with self.assertRaisesRegex(ProviderError, "incomplete_pagination"):
            self.scan()

    def test_targeted_reply_scan_stops_after_finding_root(self):
        rows, stats = self.scan(root="c" * 24)
        self.assertEqual(1, stats["root_pages"])
        self.assertEqual(3, sum(bool(x["parent_comment_id"]) for x in rows))

    def test_missing_more_flag_and_wrong_author_fail_closed(self):
        self.roots[0]["data"].pop("has_more")
        with self.assertRaisesRegex(ProviderError, "schema_changed"):
            self.scan()
        self.roots[0]["data"].update(has_more=True, user_id="other")
        with self.assertRaisesRegex(ProviderError, "identity_mismatch"):
            self.scan()

    def test_wrong_note_and_page_limit_fail_closed(self):
        self.query_note = "f" * 24
        with self.assertRaisesRegex(ProviderError, "identity_mismatch"):
            self.scan()
        self.query_note = NOTE["id"]
        with self.assertRaisesRegex(ProviderError, "incomplete_pagination"):
            self.scan(limit=1)

    def test_repeated_reply_cursor_and_incomplete_reply_count(self):
        self.children[0]["data"]["cursor"] = "d" * 24
        with self.assertRaisesRegex(ProviderError, "游标"):
            self.scan()
        self.children[0]["data"]["has_more"] = False
        with self.assertRaisesRegex(ProviderError, "未覆盖"):
            self.scan()

    def test_duplicate_terminal_root_page_is_not_complete(self):
        self.roots[1] = envelope(self.roots[0]["data"]["comments"])
        with self.assertRaisesRegex(ProviderError, "重复页面"):
            self.scan()

    def test_total_disagreement_and_invisible_root_are_not_empty_success(self):
        self.total = 6
        with self.assertRaisesRegex(ProviderError, "coverage_limited"):
            self.scan()
        with self.assertRaisesRegex(ProviderError, "目标一级评论"):
            self.scan(root="e" * 24)
