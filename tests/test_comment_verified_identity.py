"""Comment-only onboarding must recognize the freshly verified author's UID."""
import copy
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import comment_monitor as cm
from providers.douyin import DouyinProvider
from providers.registry import ProviderRegistry
from providers.wechat_channels import WeChatChannelsProvider, HOME, POST_PATH

UID = "2298429093459075"
CID = "7483043075963571475"
ACCOUNT = {"platform": "douyin", "account_name": "fixture", "platform_uid": "verified_handle"}
KEY = "douyin:fixture"


class MemorySource:
    def __init__(self, responses):
        self.responses = copy.deepcopy(responses)
        self.call_count = 0

    @contextmanager
    def session(self):
        yield self

    def get_json(self, url, params=None):
        self.call_count += 1
        return self.responses.pop(0)


class VerifiedCommentIdentityTests(unittest.TestCase):
    def setUp(self):
        self.item = {**ACCOUNT, "account_key": KEY, "content_id": CID,
                     "platform_label": "抖音", "title": "作品", "url": ""}
        self.root = {"comment_id": "101", "user_ids": ["audience"], "user": "观众",
                     "content": "问题", "reply_count": 1, "created_at": "2026-09-15T10:10:00+08:00"}
        self.reply = {"comment_id": "102", "parent_comment_id": "101", "user_ids": [UID],
                      "user": "作者", "content": "答复", "created_at": "2026-09-15T10:11:00+08:00"}
        self.timeline = {"version": 1, "timeline_started_at": "2026-09-15T10:00:00+08:00", "events": {}}
        self.state = {"baseline_done": True, "monitor_started_at": "2026-09-15T10:00:00+08:00"}
        self.args = SimpleNamespace(limit=0, max_pages=10, no_replies=False, platform=[])

    def client(self, *, actual_handle="verified_handle", expected_uid=None):
        settings = {"provider": "douyin_creator"}
        if expected_uid:
            settings["expected_uid"] = expected_uid
        profile = {"status_code": 0, "user": {"unique_id": actual_handle, "uid": UID}}
        raw = {"aweme_id": CID, "author_user_id": UID, "statistics": {"aweme_id": CID}}
        works = {"status_code": 0, "aweme_list": [raw], "items": [{"id": CID}],
                 "total": 1, "has_more": False}
        provider = DouyinProvider(ACCOUNT, settings, MemorySource([profile, works, profile]))
        client = ProviderRegistry({"accounts": {KEY: settings}})
        client.providers[KEY] = provider
        return client, provider

    def run_scan(self, client, provider, identities=None):
        def roots(cid, max_pages, include_replies):
            self.assertFalse(include_replies)
            provider._replies[(cid, self.root["comment_id"], max_pages)] = [self.reply]
            return [self.root], {"root_pages": 1, "reply_pages": 0}

        with patch.object(provider, "_scan_comments", side_effect=roots):
            return cm.check_comments(client, [self.item], self.args, state=self.state,
                                     timeline=self.timeline, official_identities=identities or {},
                                     now=datetime(2026, 9, 15, 12, tzinfo=cm.CN_TZ))

    def test_first_comment_only_scan_without_expected_uid_or_snapshot_records_author(self):
        client, provider = self.client()
        _, errors, state = self.run_scan(client, provider)
        self.assertEqual([], errors)
        self.assertEqual(1, state["last_scan"]["official_replies_added"])
        self.assertIn("douyin:102", self.timeline["events"])
        self.assertEqual(1, state["root_reply_counts"][f"douyin:{CID}:101"])
        self.assertEqual(3, provider.call_count)  # Ownership and identity checked; no dashboard read.

    def test_old_snapshot_identity_is_not_kept_after_current_author_is_verified(self):
        client, provider = self.client()
        self.reply["user_ids"] = ["old_uid"]
        identities = {KEY: {"platform": "douyin", "ids": {"old_uid"}}}
        _, errors, state = self.run_scan(client, provider, identities)
        self.assertEqual([], errors)
        self.assertEqual(0, state["last_scan"]["official_replies_added"])
        self.assertEqual({"verified_handle", UID}, identities[KEY]["ids"])

    def test_wrong_login_or_bound_uid_never_advances_comment_state(self):
        for kwargs in ({"actual_handle": "different_account"}, {"expected_uid": "999999"}):
            with self.subTest(kwargs=kwargs):
                client, provider = self.client(**kwargs)
                _, errors, state = self.run_scan(client, provider)
                self.assertTrue(any("identity_mismatch" in error for error in errors))
                self.assertEqual({}, state["root_reply_counts"])
                self.assertEqual({}, state["content_poll_at"])
                self.assertEqual({}, self.timeline["events"])

    def test_missing_successful_identity_does_not_trust_private_expected_uid(self):
        client, provider = self.client(expected_uid=UID)
        with patch.object(provider, "_ensure_owned"), \
             patch.object(provider, "_profile", return_value={"official_user_id": UID}):
            _, errors, state = self.run_scan(client, provider)
        self.assertTrue(any("identity_mismatch" in error for error in errors))
        self.assertEqual({}, state["root_reply_counts"])
        self.assertEqual({}, state["content_poll_at"])


class ChannelPage:
    def __init__(self, source):
        self.source, self.url, self.callback = source, HOME, None

    def route(self, *args):
        pass

    def on(self, event, callback):
        self.callback = callback

    def goto(self, *args, **kwargs):
        if self.callback:
            self.callback(SimpleNamespace(url="https://channels.weixin.qq.com" + POST_PATH,
                                          method="POST", post_data_json={"_log_finder_id": self.source.finder}))

    def bring_to_front(self):
        pass

    def close(self):
        pass

    def locator(self, selector):
        value = self.source.sph if selector == "#finder-uid-copy" else "1"
        locator = SimpleNamespace(wait_for=lambda **kwargs: None, inner_text=lambda: value)
        locator.first = locator
        return locator


class ChannelSource(MemorySource):
    def __init__(self, sph, finder, reply_author):
        super().__init__([])
        self.sph, self.finder = sph, finder
        self.context = SimpleNamespace(new_page=lambda: ChannelPage(self))
        self.budget = SimpleNamespace(consume=lambda *args, **kwargs: None)
        self.reply_author = reply_author

    def post_channels_readonly(self, path, body):
        self.call_count += 1
        created = int(datetime(2026, 9, 15, 10, 10, tzinfo=cm.CN_TZ).timestamp())
        return {"errCode": 0, "data": {"commentCount": 2, "downContinueFlag": 0, "comment": [
            {"commentId": "101", "username": "audience", "commentContent": "问题",
             "commentCreatetime": created, "downContinueFlag": 0, "levelTwoComment": [
                 {"commentId": "102", "username": self.reply_author, "commentContent": "答复",
                  "commentCreatetime": created + 60}]}]}}


class VerifiedChannelIdentityTests(unittest.TestCase):
    def setUp(self):
        self.sph, self.finder = "sphFixture", "v2_verified@finder"
        self.key = "wechat_channels:fixture"
        self.account = {"platform": "wechat_channels", "account_name": "fixture", "platform_uid": self.sph}
        self.item = {**self.account, "account_key": self.key, "content_id": "export/fixture",
                     "platform_label": "视频号", "title": "作品", "url": ""}
        self.timeline = {"version": 1, "timeline_started_at": "2026-09-15T10:00:00+08:00", "events": {}}
        self.state = {"baseline_done": True, "monitor_started_at": "2026-09-15T10:00:00+08:00"}
        self.args = SimpleNamespace(limit=0, max_pages=10, no_replies=False, platform=[])

    def run_scan(self, *, actual_sph=None, actual_finder=None, reply_author=None, identities=None):
        settings = {"provider": "wechat_channels_creator", "expected_sph": self.sph,
                    "expected_finder_id": self.finder}
        provider = WeChatChannelsProvider(self.account, settings,
                                         ChannelSource(actual_sph or self.sph, actual_finder or self.finder,
                                                       reply_author or self.finder))
        client = ProviderRegistry({"accounts": {self.key: settings}})
        client.providers[self.key] = provider
        result = cm.check_comments(client, [self.item], self.args, state=self.state,
                                   timeline=self.timeline, official_identities=identities or {},
                                   now=datetime(2026, 9, 15, 12, tzinfo=cm.CN_TZ))
        return result, provider

    def test_comment_only_scan_recognizes_bound_finder_without_dashboard_identity(self):
        (_, errors, state), provider = self.run_scan()
        self.assertEqual([], errors)
        self.assertEqual(1, state["last_scan"]["official_replies_added"])
        self.assertIn("wechat_channels:102", self.timeline["events"])
        self.assertEqual(self.finder, provider.verified_profile["official_user_id"])

    def test_current_verified_identity_replaces_stale_snapshot_finder(self):
        old = "v2_old@finder"
        identities = {self.key: {"platform": "wechat_channels", "ids": {old}}}
        (_, errors, state), _ = self.run_scan(reply_author=old, identities=identities)
        self.assertEqual([], errors)
        self.assertEqual(0, state["last_scan"]["official_replies_added"])
        self.assertEqual({self.sph.casefold(), self.finder}, identities[self.key]["ids"])

    def test_wrong_short_id_or_request_finder_never_advances_state(self):
        for kwargs in ({"actual_sph": "sphOther"}, {"actual_finder": "v2_other@finder"}):
            with self.subTest(kwargs=kwargs):
                (_, errors, state), provider = self.run_scan(**kwargs)
                self.assertTrue(any("identity_mismatch" in error for error in errors))
                self.assertEqual({}, state["root_reply_counts"])
                self.assertEqual({}, state["content_poll_at"])
                self.assertEqual({}, self.timeline["events"])
                self.assertEqual(0, provider.call_count)

    def test_missing_verified_profile_is_not_replaced_by_private_binding(self):
        with patch.object(WeChatChannelsProvider, "_profile", return_value={}):
            (_, errors, state), _ = self.run_scan()
        self.assertTrue(any("identity_mismatch" in error for error in errors))
        self.assertEqual({}, state["root_reply_counts"])
        self.assertEqual({}, state["content_poll_at"])
