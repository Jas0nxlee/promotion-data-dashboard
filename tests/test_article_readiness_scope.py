import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import healthcheck
from provider_setup import accounts
from providers.health import read_verification
from providers.public_articles import (annotate_public_articles, public_article_settings,
                                       record_public_article_verification)


class ArticleReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.account = {"platform": "csdn", "account_name": "测试", "platform_uid": "author",
                        "profile_url": "https://blog.csdn.net/author"}
        self.entry = {"verified_account_id": "author", "status": "ok"}

    def check(self, entry=None, records=None, error=None):
        record_public_article_verification(self.account, self.entry if entry is None else entry,
                                           records or [{"stats": {"read": 0, "comment": 0}}],
                                           error, self.directory)
        return read_verification("csdn:测试", public_article_settings(self.account), self.directory)

    def test_raw_metrics_identity_complete_scope_and_errors_gate_public_accounts(self):
        self.assertTrue(self.check()["ready"])
        for entry in ({"status": "ok"}, {"status": "ok", "verified_account_id": "other"},
                      {"status": "partial", "verified_account_id": "author"}):
            self.assertFalse(self.check(entry=entry)["ready"])
        self.assertFalse(self.check(records=[{"stats": {"read": None, "comment": 0}}])["ready"])
        self.assertFalse(self.check(error=RuntimeError("unavailable"))["ready"])

    def test_account_identity_change_invalidates_previous_public_evidence(self):
        self.assertTrue(self.check()["ready"])
        settings = public_article_settings({**self.account, "platform_uid": "new"})
        self.assertFalse(read_verification("csdn:测试", settings, self.directory)["ready"])

    def test_public_metric_annotation_does_not_change_zero_or_unknown(self):
        raw = [{"stats": {"read": 0, "comment": None}}]
        row = annotate_public_articles(self.account, raw)[0]
        self.assertEqual(raw[0]["stats"], row["stats"])
        self.assertEqual("csdn_public", row["data_source"])
        self.assertEqual("csdn_public", row["metric_provenance"]["comment"]["source"])
        self.assertNotIn("data_source", raw[0])

    def test_health_report_includes_public_articles_even_when_provider_subset_ready(self):
        catalog = {"bilibili:测试": {"platform": "bilibili"}, "csdn:测试": self.account}
        def evidence(key, settings):
            return {"ready": key.startswith("bilibili:")}
        with patch.object(healthcheck, "accounts", return_value=catalog) as listing, \
             patch.object(healthcheck, "read_verification", side_effect=evidence), \
             patch.object(healthcheck, "DATA", self.directory):
            report = healthcheck.report()
        listing.assert_called_once_with(include_public=True)
        self.assertEqual(2, report["total_accounts"])
        self.assertEqual(1, report["ready_accounts"])
        self.assertFalse(report["accounts_ready"])
        self.assertFalse(report["release_ready"])

    def test_full_catalog_covers_each_article_account(self):
        full = accounts(include_public=True)
        for platform in ("csdn", "elecfans", "baijiahao", "sohu", "toutiao"):
            self.assertTrue(any(row["platform"] == platform for row in full.values()))
        self.assertGreater(len(full), len(accounts()))
