import copy
import json
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from providers.base import ProviderError, Pages
from providers.browser import BrowserSource
from providers.wechat_channels import WeChatChannelsProvider, video_record, comment_record, COMMENTS, POST_PATH
from providers.history import quarantine_mismatched_channel
from providers.history import channel_namespace_changed, archive_channel_namespace
import comment_monitor as cm
import comment_timeline as timeline

ACCOUNT = {"platform": "wechat_channels", "account_name": "测试", "platform_uid": "sphIexample"}
SETTINGS = {"provider": "wechat_channels_creator", "expected_sph": "sphIexample", "expected_finder_id": "v2_owner@finder"}


def comment(cid, *, children=None, more=0, cursor="", author=""):
    return {"commentId": cid, "commentNickname": "演示用户", "commentContent": "示例", "username": author,
            "commentCreatetime": 1788220860, "commentLikeCount": 1,
            "levelTwoComment": children or [], "downContinueFlag": more, "lastBuff": cursor}


def response(rows, *, total=0, more=0, cursor=""):
    return {"errCode": 0, "data": {"comment": rows, "commentCount": total, "downContinueFlag": more, "lastBuff": cursor}}


class NativeSource:
    def __init__(self, responses):
        self.responses = copy.deepcopy(responses)
        self.calls = []
        self.call_count = 0
        self.settings = {}

    @contextmanager
    def session(self):
        yield self

    def post_channels_readonly(self, path, payload):
        self.calls.append((path, payload))
        self.call_count += 1
        return self.responses.pop(0)


def provider(responses):
    source = NativeSource(responses)
    result = WeChatChannelsProvider(ACCOUNT, dict(SETTINGS), source)
    result._profile = Mock(return_value={"nickname": "测试", "verified_account_id": "sphIexample"})
    result._request_template = Mock(return_value=(Mock(), {"_log_finder_id": "v2_owner@finder"}))
    result._video_catalog_total = Mock(return_value=2)
    return result, source


class ChannelsProviderTests(unittest.TestCase):
    def test_full_list_requests_are_explicit_not_preview_card_defaults(self):
        def page(cid, more):
            return {"errCode": 0, "data": {"list": [{"objectId": cid, "desc": {"description": cid}}], "totalCount": 2, "continueFlag": more}}
        p, source = provider([page("export/one", True), page("export/two", False)])
        p._profile.return_value.update({"homepage_total": 2})
        result = p.collect(max_pages=3)
        self.assertTrue(result.complete)
        self.assertEqual(2, len(result.records))
        self.assertEqual([1, 2], [args[1]["currentPage"] for args in source.calls])
        self.assertTrue(all(args[0] == POST_PATH and args[1]["pageSize"] == 20 and args[1]["stickyOrder"] is True for args in source.calls))

    def test_video_management_total_prevents_false_complete_preview_result(self):
        p, _ = provider([{"errCode": 0, "data": {"list": [{"objectId": "export/one"}], "totalCount": 1, "continueFlag": False}}])
        p._profile.return_value["homepage_total"] = 103
        p._video_catalog_total.return_value = 103
        self.assertFalse(p.collect().complete)

    def test_homepage_includes_image_posts_but_video_catalog_is_complete(self):
        p, _ = provider([{"errCode": 0, "data": {"list": [{"objectId": "export/one"}], "totalCount": 1, "continueFlag": False}}])
        p._profile.return_value["homepage_total"] = 7
        p._video_catalog_total.return_value = 1
        result = p.collect()
        self.assertTrue(result.complete)
        self.assertEqual(7, result.profile["homepage_total"])
        self.assertEqual(1, result.profile["video_catalog_total"])

    def test_video_total_change_during_pagination_does_not_certify_coverage(self):
        p, _ = provider([
            {"errCode": 0, "data": {"list": [{"objectId": "export/one"}], "totalCount": 2, "continueFlag": True}},
            {"errCode": 0, "data": {"list": [{"objectId": "export/two"}], "totalCount": 3, "continueFlag": False}}])
        with self.assertRaisesRegex(ProviderError, "incomplete_pagination"):
            p.collect()

    def test_missing_video_management_evidence_does_not_fall_back_to_home(self):
        p, source = provider([])
        p._video_catalog_total.side_effect = ProviderError("schema_changed", "missing tab")
        with self.assertRaisesRegex(ProviderError, "schema_changed"):
            p.collect()
        self.assertFalse(source.calls)

    def test_opaque_ids_and_metrics_keep_their_actual_meaning(self):
        row = video_record({"objectId": "export/opaque", "createTime": 1788220860,
                            "likeCount": 9, "favCount": 11, "readCount": 784, "commentCount": 0,
                            "desc": {"description": "示例", "media": [{"videoPlayLen": 95}]}}, {"nickname": "测试"})
        self.assertEqual("export/opaque", row["video_id"])
        self.assertEqual(9, row["stats"]["like"])
        self.assertIsNone(row["stats"]["collect"])
        self.assertEqual(11, row["extra_metrics"]["favCount"])
        self.assertEqual("", row["url"])
        with self.assertRaisesRegex(ProviderError, "schema_changed"):
            video_record({"objectId": "123"}, {"nickname": "测试"})

    def test_profiles_are_case_sensitive(self):
        source = Mock()
        page = source.context.new_page.return_value
        page.locator.return_value.inner_text.return_value = "sphlexample"
        page.url = "https://channels.weixin.qq.com/platform"
        p = WeChatChannelsProvider(ACCOUNT, SETTINGS, source)
        with self.assertRaisesRegex(ProviderError, "identity_mismatch"):
            p._profile()

    def test_roots_and_nested_reply_pagination(self):
        r1 = comment("r1", children=[comment("c1")], more=1, cursor="reply-next")
        r2 = comment("r2", children=[comment("c3")])
        p, source = provider([
            response([r1], total=5, more=1, cursor="root-next"),
            response([comment("c2")], total=0),
            response([r2], total=5),
        ])
        rows, stats = p.comments({"content_id": "export/v1"}, max_pages=5)
        self.assertEqual({"r1", "r2", "c1", "c2", "c3"}, {r["comment_id"] for r in rows})
        self.assertEqual(2, stats["root_pages"])
        self.assertEqual(1, stats["reply_pages"])
        self.assertTrue(stats["comments_complete"])
        self.assertTrue(stats["replies_complete"])
        self.assertEqual(3, stats["expected_replies"])
        self.assertEqual("r1", source.calls[1][1]["rootCommentId"])
        self.assertEqual("root-next", source.calls[2][1]["lastBuff"])
        self.assertEqual({COMMENTS}, {path for path, _ in source.calls})

    def test_no_replies_skips_extra_thread_requests_but_exposes_unknown_count(self):
        r1 = comment("r1", children=[comment("c1")], more=1, cursor="next")
        p, source = provider([response([r1], total=3), response([comment("c2")])])
        roots, stats = p.comments({"content_id": "export/v1"}, include_replies=False)
        self.assertTrue(stats["comments_complete"])
        self.assertFalse(stats["replies_complete"])
        self.assertEqual(1, len(source.calls))
        self.assertTrue(roots[0]["reply_count_is_lower_bound"])
        replies, used = p.replies({"content_id": "export/v1"}, "r1")
        self.assertEqual(["c1", "c2"], [r["comment_id"] for r in replies])
        self.assertEqual(1, used)
        p.replies({"content_id": "export/v1"}, "r1")
        self.assertEqual(2, len(source.calls))

    def test_missing_or_repeated_cursor_is_not_completion(self):
        p, _ = provider([response([comment("r1")], total=2, more=1, cursor="")])
        with self.assertRaisesRegex(ProviderError, "incomplete_pagination"):
            p.comments({"content_id": "export/v1"})
        self.assertEqual({}, p._comments)

    def test_complete_empty_comments_allow_authorization_without_invented_replies(self):
        p, _ = provider([response([], total=0)])
        rows, stats = p.comments({"content_id": "export/v1"})
        self.assertEqual([], rows)
        self.assertTrue(stats["comments_complete"])
        self.assertTrue(stats["replies_complete"])
        self.assertEqual(0, stats["expected_replies"])

    def test_truncated_reply_pages_never_return_complete_evidence(self):
        p, _ = provider([response([comment("r1", more=1, cursor="next")], total=3),
                         response([comment("c1")], more=1, cursor="next-again")])
        with self.assertRaisesRegex(ProviderError, "incomplete_replies"):
            p.comments({"content_id": "export/v1"}, max_pages=1)
        self.assertEqual({}, p._comments)

    def test_total_mismatch_does_not_advance(self):
        p, _ = provider([response([comment("r1")], total=2)])
        with self.assertRaisesRegex(ProviderError, "数量"):
            p.comments({"content_id": "export/v1"})

    def test_legacy_numeric_id_requires_identity_migration(self):
        p, source = provider([])
        with self.assertRaisesRegex(ProviderError, "migration_required"):
            p.comments({"content_id": "12345678901234567890"})
        self.assertFalse(source.calls)

    def test_mutating_endpoint_is_rejected_before_request(self):
        source = BrowserSource(ACCOUNT, SETTINGS)
        source.context = Mock()
        with self.assertRaisesRegex(ProviderError, "invalid_operation"):
            source.post_channels_readonly("/micro/interaction/cgi-bin/mmfinderassistant-bin/comment/update_feed_comment", {})
        source.context.request.post.assert_not_called()

    def test_official_author_requires_returned_stable_identity(self):
        raw = comment("r1", author="v2_owner@finder")
        parsed = comment_record(raw, "root")
        item = {**ACCOUNT, "account_key": "wechat_channels:测试"}
        identities = {"wechat_channels:测试": {"ids": {"v2_owner@finder"}}}
        self.assertTrue(cm.is_official_author(parsed, item, identities))
        parsed["user_ids"] = []
        parsed["user"] = "测试"
        self.assertFalse(cm.is_official_author(parsed, item, identities))

    def test_confirmed_wrong_account_cache_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp, patch("providers.history.DATA", Path(tmp)):
            old = {**ACCOUNT, "account_key": "wechat_channels:测试", "platform_uid": "v2_wrong@finder"}
            records = [{"video_id": "old"}]
            path = quarantine_mismatched_channel(old, records, {"official_user_id": "v2_correct@finder", "verified_account_id": "sphIexample"})
            self.assertEqual(records, json.loads(path.read_text())["previous_records"])
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertIsNone(quarantine_mismatched_channel(old, records, {"official_user_id": "v2_wrong@finder"}))

    def test_binding_checks_short_id_before_and_after_capture(self):
        p, _ = provider([])
        p._profile = Mock(side_effect=[{"verified_account_id": "sphIexample"}, {"verified_account_id": "sphIexample"}])
        p._request_template = Mock(return_value=(Mock(), {"_log_finder_id": "v2_new@finder", "rawKeyBuff": "fake-session-secret"}))
        bound = p.bind_from_login()
        self.assertEqual("v2_new@finder", bound["expected_finder_id"])
        self.assertNotIn("rawKeyBuff", bound)
        self.assertEqual(2, p._profile.call_count)

    def test_binding_rejects_an_account_switch_during_capture(self):
        p, _ = provider([])
        p._profile = Mock(side_effect=[{"verified_account_id": "sphA"}, {"verified_account_id": "sphB"}])
        with self.assertRaisesRegex(ProviderError, "identity_mismatch"):
            p.bind_from_login()

    def test_namespace_transition_is_detected_and_original_catalog_archived(self):
        old = [{"video_id": "123", "stats": {"like": 7}}]
        self.assertTrue(channel_namespace_changed(old, [{"video_id": "export/new"}]))
        with tempfile.TemporaryDirectory() as tmp, patch("providers.history.DATA", Path(tmp)):
            path = archive_channel_namespace(ACCOUNT, old)
            self.assertEqual(old, json.loads(path.read_text())["previous_records"])

    def test_seen_legacy_comment_ids_are_not_alerted_again_under_export_id(self):
        item = {**ACCOUNT, "account_key": "wechat_channels:测试", "platform_label": "视频号", "content_id": "export/new", "title": "示例"}
        roots = [{"comment_id": cid, "created_at": "2026-09-02T00:00:00+08:00", "reply_count": 0, "user_ids": []} for cid in ("old", "new")]
        client = SimpleNamespace(call_count=0, fetch_roots=lambda *_args: (roots, 1))
        args = SimpleNamespace(platform=[], limit=0, max_pages=5, no_replies=True, tiered_polling=False)
        state = {"baseline_done": True, "monitor_started_at": "2026-09-01T00:00:00+08:00", "seen_comments": {"wechat_channels:123": ["old"]}}
        items, errors, _state = cm.check_comments(client, [item], args, state=state)
        self.assertEqual([], errors)
        self.assertEqual(["new"], [x["comment_id"] for x in items[0]["comments"]])

    def test_stable_comment_rebinds_timeline_without_creating_a_duplicate(self):
        old_item = {**ACCOUNT, "account_key": "wechat_channels:测试", "content_id": "123", "title": "旧标题", "url": ""}
        root = {"comment_id": "same", "created_at": "2026-09-02T00:00:00+08:00", "content": "问题"}
        state = {"timeline_started_at": "2026-09-01T00:00:00+08:00", "events": {}}
        timeline.record_comment(state, old_item, root, observed_at="2026-09-02T00:00:00+08:00")
        self.assertFalse(timeline.record_comment(state, {**old_item, "content_id": "export/new", "title": "新标题"}, root, observed_at="2026-09-03T00:00:00+08:00"))
        self.assertEqual(1, len(state["events"]))
        event = next(iter(state["events"].values()))
        self.assertEqual("export/new", event["content_id"])
        self.assertEqual("123", event["id_migrated_from"])

    def test_truncated_reply_count_is_rechecked_even_when_lower_bound_did_not_grow(self):
        item = {**ACCOUNT, "account_key": "wechat_channels:测试", "platform_label": "视频号", "content_id": "export/v", "title": "示例"}
        root = {"comment_id": "r", "created_at": "2026-09-02T00:00:00+08:00", "reply_count": 2,
                "reply_count_is_lower_bound": True, "user_ids": []}
        replies = [{"comment_id": str(i), "parent_comment_id": "r", "created_at": "2026-09-02T01:00:00+08:00", "user_ids": []} for i in range(3)]
        fetch = Mock(return_value=(replies, 1))
        client = SimpleNamespace(call_count=0, fetch_roots=lambda *_args: ([root], 1), fetch_replies=fetch)
        args = SimpleNamespace(platform=[], limit=0, max_pages=5, no_replies=False, tiered_polling=False)
        state = {"baseline_done": True, "monitor_started_at": "2026-09-01T00:00:00+08:00", "seen_comments": {"wechat_channels:export/v": ["r"]},
                 "full_scan_baselines": ["wechat_channels:export/v"], "root_reply_counts": {"wechat_channels:export/v:r": 2}}
        events = {"timeline_started_at": "2026-09-01T00:00:00+08:00", "events": {}}
        _, errors, updated = cm.check_comments(client, [item], args, state=state, timeline=events)
        self.assertEqual([], errors)
        fetch.assert_called_once()
        self.assertEqual(3, updated["root_reply_counts"]["wechat_channels:export/v:r"])
