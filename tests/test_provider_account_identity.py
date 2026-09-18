import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from providers.registry import ProviderRegistry


class AccountIdentityTests(unittest.TestCase):
    def test_comment_only_run_resolves_identity_without_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            account = {"platform": "douyin", "account_name": "fixture", "platform_uid": "canonical_handle"}
            (root / "config/accounts.json").write_text(json.dumps({"accounts": [account]}))
            with patch("providers.registry.ROOT", root):
                registry = ProviderRegistry({"accounts": {"douyin:fixture": {"provider": "douyin_creator"}}})
                provider = registry.get({"platform": "douyin", "account_name": "fixture", "content_id": "123"})
                self.assertEqual("canonical_handle", provider.account["platform_uid"])
                self.assertEqual("canonical_handle", provider.browser.account["platform_uid"])

    def test_article_identity_comes_from_config_not_snapshot_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            account = {"platform": "xiaohongshu", "account_name": "fixture", "platform_uid": "native_uid", "provided_id": "red_number"}
            (root / "config/article_accounts.json").write_text(json.dumps({"accounts": [account]}))
            with patch("providers.registry.ROOT", root):
                registry = ProviderRegistry({"accounts": {"xiaohongshu:fixture": {"provider": "xiaohongshu_creator"}}})
                provider = registry.get({**account, "provided_id": "stale_snapshot"})
                self.assertEqual("red_number", provider.account["provided_id"])
