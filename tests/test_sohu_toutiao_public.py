"""Identity and pagination invariants for the two public browser collectors."""
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from fetch_article_data import SohuCollector, ToutiaoCollector

SOHU = {"platform": "sohu", "platform_uid": "122486766", "account_name": "fixture", "business_line": "test"}
TOUTIAO = {"platform": "toutiao", "platform_uid": "6733312456", "account_name": "fixture", "business_line": "test"}
BLOCK = "FeedSlideloadAuthor_data2"


def sohu_row(cid):
    return {"id": cid, "url": f"/a/{cid}_122486766", "resourceType": "1", "title": "article",
            "extraInfoList": [{"image": "pv", "text": "12阅读"}, {"image": "comment", "text": "0评论"},
                              {"image": "time", "text": "2026.09.16"}]}


def sohu_page(total=2, following=None):
    profile = {"id": "122486766", "column_5_text": total, "title": "fixture"}
    state = {"request": {"mkey": {"mkey": "122486766"}}, "blocks": {
        "p": {"comp": {"compName": "BriefIntroductionCard"}, "param": {"data": {"list": [profile]}}},
        "f": {"comp": {"compName": "FeedSlideloadAuthor"}, "param": {"data2": {
            "list": [sohu_row(101)] if total else [],
            "reqParam": {"tplCompKey": BLOCK, "content": {"productId": "325", "size": 20}}}}}}}
    page = Mock()
    page.evaluate.return_value = state
    next_rows = [sohu_row(102)] if following is None else following
    response = SimpleNamespace(url="https://odin.sohu.com/odin/api/blockdata", status=200,
        request=SimpleNamespace(post_data_json={"resourceList": [{"tplCompKey": BLOCK, "context": {"mkey": "122486766"}, "content": {"page": 2}}]}),
        json=lambda: {"code": 0, "success": True, "data": {BLOCK: {"list": next_rows}}})
    @contextmanager
    def expect(predicate, **kwargs):
        assert predicate(response)
        yield SimpleNamespace(value=response)
    page.expect_response.side_effect = expect
    return page, state


def toutiao_row(cid="7685708916088439359", uid="6733312456"):
    return {"group_id": cid, "title": "article", "publish_time": 1789443362,
            "itemCell": {"articleBase": {"gidStr": cid}, "userInfo": {"userID": int(uid), "mediaID": int(uid)},
                         "itemCounter": {"readCount": 3, "commentCount": 0}}}


def payload(rows, more=False, cursor=100):
    return {"message": "success", "data": rows, "has_more": more, "next": {"max_behot_time": cursor}}


class SohuPublicTests(unittest.TestCase):
    def test_full_partial_and_empty_catalog_keep_verified_identity(self):
        for total, cap, status, count in ((2, 2, "ok", 2), (2, 1, "partial", 1), (0, 2, "ok", 0)):
            with self.subTest(total=total, cap=cap):
                page, _ = sohu_page(total)
                entry, rows = SohuCollector(SimpleNamespace(call_count=0), cap)._collect_page(page, SOHU)
                self.assertEqual(status, entry["status"]); self.assertEqual(count, len(rows))
                self.assertEqual("122486766", entry["verified_account_id"])
                self.assertEqual("sohu_public", entry["data_source"])

    def test_profile_and_each_article_require_exact_author_id(self):
        page, state = sohu_page()
        state["blocks"]["p"]["param"]["data"]["list"][0]["id"] = "other"
        with self.assertRaisesRegex(RuntimeError, "作者 ID"):
            SohuCollector(SimpleNamespace(call_count=0))._collect_page(page, SOHU)
        row = sohu_row(101); row["url"] = "/a/101_999"
        with self.assertRaisesRegex(RuntimeError, "精确作者"):
            SohuCollector._article(row, SOHU)

    def test_empty_or_repeated_next_page_fails_instead_of_completing(self):
        for rows in ([], [sohu_row(101)]):
            page, _ = sohu_page(following=rows)
            with self.assertRaises(RuntimeError):
                SohuCollector(SimpleNamespace(call_count=0))._collect_page(page, SOHU)

    def test_rounded_metrics_and_unexposed_fields_remain_unknown(self):
        row = sohu_row(101); row["extraInfoList"][0]["text"] = "1.2万阅读"
        result = SohuCollector._article(row, SOHU)
        self.assertIsNone(result["stats"]["read"])
        self.assertEqual(0, result["stats"]["comment"])
        self.assertIsNone(result["stats"]["like"])


class ToutiaoPublicTests(unittest.TestCase):
    def collector(self, pages, cap=10):
        p = ToutiaoCollector(max_pages=cap)
        p._ensure_browser = Mock()
        p._context = Mock()
        page = p._context.new_page.return_value
        page.url = "https://www.toutiao.com/c/user/6733312456/"
        page.locator.return_value.first.count.return_value = 1
        page.locator.return_value.first.inner_text.return_value = "fixture"
        page.locator.return_value.all_inner_texts.return_value = []
        page.get_by_text.return_value.count.return_value = 1
        responses = [SimpleNamespace(url="https://www.toutiao.com/api/pc/list/user/feed?category=pc_profile_article",
                                     status=200, json=lambda value=value: value) for value in pages]
        initial = iter([responses[0], responses[0]])
        @contextmanager
        def expect(predicate, **kwargs):
            response = next(initial); assert predicate(response)
            yield SimpleNamespace(value=response)
        page.expect_response.side_effect = expect
        p._next_feed = Mock(side_effect=responses[1:])
        return p

    def test_numeric_stable_author_binding_and_long_ids_survive(self):
        row = ToutiaoCollector._article_from_item(toutiao_row(), TOUTIAO)
        self.assertEqual("6733312456", row["source_author_id"])
        self.assertEqual("7685708916088439359", row["article_id"])
        with self.assertRaisesRegex(RuntimeError, "精确作者"):
            ToutiaoCollector._article_from_item(toutiao_row(uid="111"), TOUTIAO)
        bad = toutiao_row(); bad["itemCell"]["articleBase"]["gidStr"] = "different"
        with self.assertRaisesRegex(RuntimeError, "ID 不一致"):
            ToutiaoCollector._article_from_item(bad, TOUTIAO)

    def test_distinct_verified_user_and_media_ids_are_both_required(self):
        account={**TOUTIAO,'expected_media_id':'1866310957476868'}
        row=toutiao_row()
        row['itemCell']['userInfo']['mediaID']=1866310957476868
        parsed=ToutiaoCollector._article_from_item(row,account)
        self.assertEqual(account['platform_uid'],parsed['source_author_id'])
        self.assertEqual(account['expected_media_id'],parsed['source_media_id'])
        for field,value in [('userID',123),('mediaID',123)]:
            wrong=toutiao_row();wrong['itemCell']['userInfo'].update(row['itemCell']['userInfo']);wrong['itemCell']['userInfo'][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(RuntimeError,'精确作者'):
                ToutiaoCollector._article_from_item(wrong,account)
        with self.assertRaisesRegex(RuntimeError,'精确作者'):
            ToutiaoCollector._article_from_item(row,TOUTIAO)

    def test_two_pages_finish_or_cap_as_partial(self):
        pages = [payload([toutiao_row()], True, 100), payload([toutiao_row("7685708916088439360")])]
        entry, rows = self.collector(pages).collect(TOUTIAO)
        self.assertEqual("ok", entry["status"]); self.assertEqual(2, len(rows))
        self.assertEqual("6733312456", entry["verified_account_id"])
        self.assertEqual("toutiao_public", entry["data_source"])
        entry, rows = self.collector(pages, cap=1).collect(TOUTIAO)
        self.assertEqual("partial", entry["status"]); self.assertEqual(1, len(rows))

    def test_normal_token_route_requires_numeric_author_and_cannot_switch_during_scan(self):
        p = self.collector([payload([toutiao_row()])])
        p._context.new_page.return_value.url = "https://www.toutiao.com/c/user/token/observed-profile-token=/?tab=article"
        entry, rows = p.collect(TOUTIAO)
        self.assertEqual("6733312456", entry["verified_account_id"])
        p = self.collector([payload([toutiao_row(uid="111")])])
        p._context.new_page.return_value.url = "https://www.toutiao.com/c/user/token/observed-profile-token=/"
        with self.assertRaisesRegex(RuntimeError, "精确作者"):
            p.collect(TOUTIAO)
        pages = [payload([toutiao_row()], True), payload([toutiao_row("7685708916088439360")])]
        p = self.collector(pages)
        original = p._next_feed
        def changed(page):
            page.url = "https://www.toutiao.com/c/user/token/different-profile/"
            return next(original.side_effect)
        p._next_feed = changed
        with self.assertRaisesRegex(RuntimeError, "身份在采集期间"):
            p.collect(TOUTIAO)

    def test_missing_or_invalid_end_marker_is_rejected(self):
        for value in (None, "false", 2):
            data = payload([toutiao_row()]); data["has_more"] = value
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "结束标记"):
                self.collector([data]).collect(TOUTIAO)

    def test_duplicate_terminal_page_and_repeated_cursor_are_not_complete(self):
        first = payload([toutiao_row()], True, 100)
        for second in (payload([toutiao_row()]), payload([toutiao_row("7685708916088439360")], True, 100)):
            with self.assertRaises(RuntimeError):
                self.collector([first, second]).collect(TOUTIAO)

    def test_empty_page_with_more_and_missing_cursor_fail_closed(self):
        for data in (payload([], True), payload([toutiao_row()], True, None)):
            with self.assertRaisesRegex(RuntimeError, "游标"):
                self.collector([data]).collect(TOUTIAO)

    def test_empty_title_in_article_category_is_not_silently_dropped(self):
        bad = toutiao_row("7685708916088439360"); bad["title"] = ""
        with self.assertRaisesRegex(RuntimeError, "无标题"):
            self.collector([payload([toutiao_row(), bad])]).collect(TOUTIAO)

    def test_counts_require_nonnegative_integer_and_profile_abbreviations_stay_unknown(self):
        for value in ("1.2万", -1, 1.2, 1.0, True, "1.0", "-3"):
            with self.subTest(value=value):
                raw = toutiao_row(); raw["itemCell"]["itemCounter"]["readCount"] = value
                raw["read_count"] = value
                self.assertIsNone(ToutiaoCollector._article_from_item(raw, TOUTIAO)["stats"]["read"])
        for value, expected in ((0, 0), ("0", 0), (1234, 1234), ("1,234", 1234)):
            self.assertEqual(expected, ToutiaoCollector._exact_count(value))
        page = Mock(); page.locator.return_value.all_inner_texts.return_value = ["7.6万粉丝", "110.8万获赞", "2关注"]
        self.assertEqual({"followers": None, "lifetime_likes": None, "following": 2}, ToutiaoCollector._profile_metrics(page))
