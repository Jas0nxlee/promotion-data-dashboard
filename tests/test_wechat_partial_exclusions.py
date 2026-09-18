import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import fetch_article_data as articles
from providers.base import Collection, ProviderError


class WeChatPartialExclusionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="promotion-partial-exclusions-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.accounts = [{"platform": "wechat_service", "account_name": name, "platform_uid": "gh_" + name,
                          "business_line": "业务", "collector": "wechat_mp"} for name in ("A", "B")]
        self.key = "wechat_service:A"
        self.config, self.snapshot = self.folder / "accounts.json", self.folder / "old.json"
        self.config.write_text(json.dumps({"accounts": self.accounts}))
        self.old = [articles.attach_account({"article_id": cid, "title": cid, "stats": {"read": 100}}, account)
                    for account in self.accounts for cid in ("deleted", "video", "unseen")]
        self.snapshot.write_text(json.dumps({"updated_at": "2026-09-16T10:00:00+08:00", "articles": self.old,
                                            "accounts": [{**articles.base_account(account), "covered_articles": 3}
                                                         for account in self.accounts]}))
        self.excluded = [{"article_id": "deleted", "reason": "deleted"},
                         {"article_id": "video", "reason": "standalone_channels_video"}]

    def scan(self, *, source="wechat_browser", verified="gh_A", exclusions=None, provider_error=None, merge_error=False):
        collection = Collection({"verified_account_id": verified, "total": None,
                                 "excluded_contents": self.excluded if exclusions is None else exclusions},
                                [{"article_id": "current", "title": "新文章", "stats": {"comment": 0}}],
                                complete=False, source=source)
        provider = SimpleNamespace(collect=Mock(return_value=collection, side_effect=provider_error))
        registry = SimpleNamespace(config={"accounts": {self.key: {"provider": "wechat_browser"}}}, call_count=0,
                                   get=lambda account: provider)
        args = SimpleNamespace(out=self.snapshot, debug=False, public_interval=0, manual_input=self.folder / "manual.json",
                               toutiao_pages=1, toutiao_timeout=1, toutiao_headed=False, max_pages=1,
                               wechat_pages=1, only=[self.key])
        merge = patch.object(articles, "merge_records", side_effect=RuntimeError("merge failed")) if merge_error else \
            patch.object(articles, "merge_records", wraps=articles.merge_records)
        with patch.object(articles, "CONFIG_PATH", self.config), patch.object(articles, "ProviderRegistry", return_value=registry), \
             patch.object(articles, "record_verification"), merge:
            return articles.collect(args)

    def ids(self, result, name="A"):
        return {row["article_id"] for row in result["articles"] if row["account_key"] == "wechat_service:" + name}

    def test_verified_partial_excludes_inspected_ids_but_preserves_unseen_and_other_account(self):
        result = self.scan()
        self.assertEqual({"current", "unseen"}, self.ids(result))
        self.assertEqual({"deleted", "video", "unseen"}, self.ids(result, "B"))
        self.assertEqual("partial", result["accounts"][0]["status"])
        self.assertEqual(2, result["accounts"][0]["covered_articles"])
        self.assertEqual(6, len(json.loads(self.snapshot.read_text())["articles"]))

    def test_unknown_reason_or_explicit_other_account_exclusion_does_not_remove_cache(self):
        result = self.scan(exclusions=[{"article_id": "deleted", "reason": "not_observed"},
                                       {"article_id": "video", "reason": "deleted", "account_key": "wechat_service:B"}])
        self.assertEqual({"current", "deleted", "video", "unseen"}, self.ids(result))

    def test_other_provider_partial_metadata_does_not_activate_this_exclusion_rule(self):
        result = self.scan(source="wechat_official")
        self.assertEqual({"current", "deleted", "video", "unseen"}, self.ids(result))

    def test_unverified_collection_keeps_original_cache(self):
        result = self.scan(verified=None)
        self.assertEqual({"deleted", "video", "unseen"}, self.ids(result))
        self.assertEqual("stale", result["accounts"][0]["status"])

    def test_collection_or_later_merge_failure_keeps_every_original_cached_row(self):
        for kwargs in ({"provider_error": ProviderError("identity_mismatch", "wrong account")}, {"merge_error": True}):
            with self.subTest(kwargs=kwargs):
                result = self.scan(**kwargs)
                self.assertEqual({"deleted", "video", "unseen"}, self.ids(result))
                self.assertEqual("stale", result["accounts"][0]["status"])
