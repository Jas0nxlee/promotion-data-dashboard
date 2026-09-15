"""Unverified official credentials must never supply even partial snapshot rows."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import fetch_article_data as articles
from providers.base import Collection, ProviderError
from providers.wechat_mp import WeChatOfficialProvider

ACCOUNT = {"platform": "wechat_service", "account_name": "fixture", "platform_uid": "gh_fixture",
           "business_line": "业务", "collector": "wechat_mp"}
KEY = "wechat_service:fixture"
SETTINGS = {"provider": "wechat_official", "bound_account_key": KEY,
            "bound_platform_uid": "gh_fixture", "expected_app_id": "fixture-app",
            "app_id_env": "TEST_OFFICIAL_APP", "app_secret_env": "TEST_OFFICIAL_SECRET",
            "access_token_env": "TEST_OFFICIAL_TOKEN", "history_scope_verified": True}


class WeChatOfficialIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="promotion-official-identity-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        env = patch.dict(os.environ, {"TEST_OFFICIAL_APP": "fixture-app",
                                     "TEST_OFFICIAL_SECRET": "fake-secret", "TEST_OFFICIAL_TOKEN": ""})
        env.start(); self.addCleanup(env.stop)
        sessions = patch("providers.credentials.SESSIONS", self.folder / "sessions")
        sessions.start(); self.addCleanup(sessions.stop)

    def test_static_token_cannot_claim_complete_history_or_send_requests(self):
        http = Mock(call_count=0)
        with patch.dict(os.environ, {"TEST_OFFICIAL_TOKEN": "fake-static-token"}):
            with self.assertRaisesRegex(ProviderError, "setup_required.*静态"):
                WeChatOfficialProvider(ACCOUNT, SETTINGS, http).collect()
        http.request.assert_not_called()

    def test_original_id_binding_and_account_key_are_both_required(self):
        for account, settings in (({**ACCOUNT, "platform_uid": "gh_replacement"}, SETTINGS),
                                  (ACCOUNT, {**SETTINGS, "bound_platform_uid": ""}),
                                  (ACCOUNT, {**SETTINGS, "bound_account_key": "wechat_service:other"})):
            with self.subTest(account=account, settings=settings):
                http = Mock(call_count=0)
                with self.assertRaisesRegex(ProviderError, "identity_mismatch"):
                    WeChatOfficialProvider(account, settings, http).collect()
                http.request.assert_not_called()

    def test_bound_appid_renewal_and_response_produce_verified_complete_result(self):
        http = Mock(call_count=0)
        http.request.side_effect = [
            {"access_token": "fake-renewed-token", "expires_in": 7200},
            {"total_count": 1, "item": [{"article_id": "message", "content": {"news_item": [
                {"title": "文章", "url": "https://mp.weixin.qq.com/s?mid=100&idx=1"}]}}]},
        ]
        result = WeChatOfficialProvider(ACCOUNT, SETTINGS, http).collect()
        self.assertEqual("fixture-app", result.profile["verified_account_id"])
        self.assertTrue(result.complete)
        self.assertEqual("100-1", result.records[0]["article_id"])
        self.assertEqual(2, http.request.call_count)

    def test_different_runtime_appid_fails_before_any_request(self):
        http = Mock(call_count=0)
        with patch.dict(os.environ, {"TEST_OFFICIAL_APP": "other-app"}):
            with self.assertRaisesRegex(ProviderError, "identity_mismatch"):
                WeChatOfficialProvider(ACCOUNT, SETTINGS, http).collect()
        http.request.assert_not_called()

    def pipeline(self, *, verified, complete):
        config, snapshot = self.folder / "accounts.json", self.folder / "snapshot.json"
        public = {"platform": "csdn", "account_name": "public", "platform_uid": "public",
                  "business_line": "业务", "collector": "csdn"}
        config.write_text(json.dumps({"accounts": [ACCOUNT, public]}))
        old = articles.attach_account({"article_id": "100-1", "title": "旧文章", "stats": {"read": 42}}, ACCOUNT)
        snapshot.write_text(json.dumps({"updated_at": "2026-09-01T00:00:00+08:00", "articles": [old],
                                       "accounts": [{**articles.base_account(ACCOUNT), "covered_articles": 1}]}))
        result = Collection({"verified_account_id": verified, "total": 1},
                            [{"article_id": "200-1", "title": "未知账号文章", "stats": {}}], complete=complete)
        registry = SimpleNamespace(config={"accounts": {KEY: SETTINGS}}, call_count=0,
                                   get=lambda account: SimpleNamespace(collect=lambda **kwargs: result))
        args = SimpleNamespace(out=snapshot, debug=False, public_interval=0, manual_input=self.folder / "manual.json",
                               toutiao_pages=1, toutiao_timeout=1, toutiao_headed=False, max_pages=1,
                               wechat_pages=1, only=[])
        public_entry = {**articles.base_account(public), "covered_articles": 1, "total_articles": 1}
        public_row = articles.attach_account({"article_id": "public-1", "stats": {}}, public)
        with patch.object(articles, "CONFIG_PATH", config), patch.object(articles, "ProviderRegistry", return_value=registry), \
             patch.object(articles, "record_verification"), \
             patch.object(articles.CsdnCollector, "collect", return_value=(public_entry, [public_row])):
            collected = articles.collect(args)
        self.assertIn("public-1", [row["article_id"] for row in collected["articles"]])
        return collected

    def test_unverified_complete_and_partial_results_preserve_cache_without_pollution(self):
        for complete in (True, False):
            with self.subTest(complete=complete):
                result = self.pipeline(verified=None, complete=complete)
                self.assertEqual("stale", result["accounts"][0]["status"])
                rows = [r for r in result["articles"] if r["account_key"] == KEY]
                self.assertEqual(["100-1"], [r["article_id"] for r in rows])
                self.assertEqual(42, rows[0]["stats"]["read"])

    def test_verified_partial_result_still_merges_with_cache(self):
        result = self.pipeline(verified="fixture-app", complete=False)
        self.assertEqual("partial", result["accounts"][0]["status"])
        rows = [r for r in result["articles"] if r["account_key"] == KEY]
        self.assertEqual({"100-1", "200-1"}, {r["article_id"] for r in rows})

