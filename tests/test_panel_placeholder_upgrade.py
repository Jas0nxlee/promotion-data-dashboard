import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import control_panel as panel_module
from providers.settings import SettingsStore


class PlaceholderUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="promotion-panel-upgrade-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = SettingsStore(self.directory / "providers.json")
        self.accounts = {f"{platform}:fixture": {"platform": platform, "account_name": "fixture", "platform_uid": "canonical"}
                         for platform in (*panel_module.NATIVE_PROVIDERS, "wechat_service", "wechat_subscription")}
        patches = [patch.object(panel_module, "SettingsStore", return_value=self.store),
                   patch.object(panel_module, "accounts", return_value=self.accounts),
                   patch.object(panel_module, "SESSIONS", self.directory / "sessions"),
                   patch.object(panel_module, "PROVIDER_CONFIG", self.store.path),
                   patch.object(panel_module, "ThreadPoolExecutor",
                                return_value=SimpleNamespace(submit=lambda callback: callback())),
                   patch.object(panel_module.sys, "platform", "darwin"),
                   patch.object(panel_module.socket, "create_connection"),
                   patch.object(panel_module.subprocess, "run", return_value=SimpleNamespace(returncode=0))]
        mocks = [patcher.start() for patcher in patches]
        for patcher in patches:
            self.addCleanup(patcher.stop)
        self.commands = mocks[-1]
        self.panel = panel_module.Panel()
        self.placeholder = {"provider": "browser", "channel": "chrome", "cdp_url": "http://127.0.0.1:62222",
                            "session_mode": "portable", "expected_uid": "bound-uid",
                            "expected_sph": "bound-sph", "expected_finder_id": "bound-finder",
                            "comment_identity_compatible": False}

    def test_all_native_platform_placeholders_upgrade_without_losing_metadata(self):
        for platform, native in panel_module.NATIVE_PROVIDERS.items():
            with self.subTest(platform=platform):
                account = self.accounts[f"{platform}:fixture"]
                settings = copy.deepcopy(self.placeholder)
                upgraded = panel_module.onboarding_settings(account, settings)
                self.assertEqual({**settings, "provider": native}, upgraded)
                self.assertEqual(self.placeholder, settings)

    def test_custom_or_unknown_recipe_fields_are_never_auto_upgraded(self):
        for field in ("workflows", "profile", "content_mapping", "comment_mapping", "reply_mapping",
                      "export", "export_mapping", "collection_mode", "variables", "future_recipe"):
            for value in ({}, {"custom": "mapping"}):
                with self.subTest(field=field, value=value):
                    settings = {**self.placeholder, field: value}
                    upgraded = panel_module.onboarding_settings(self.accounts["zhihu:fixture"], settings)
                    self.assertEqual(settings, upgraded)
        # Generic browser recipes may rely on the registry's default provider.
        self.assertEqual("browser", panel_module.onboarding_settings(self.accounts["zhihu:fixture"],
                                                                    {"profile": {"expected_id": "custom"}})["provider"])

    def test_identity_aliases_prevent_login_and_probe_upgrade_even_when_empty(self):
        for field, populated in (("content_aliases", {"native-id": "legacy-id"}),
                                 ("comment_aliases", {"content-id": {"native-comment": "legacy-comment"}})):
            for value in ({}, populated):
                with self.subTest(field=field, value=value):
                    key = "zhihu:fixture"
                    settings = {**self.placeholder, field: value}
                    self.store.update(key, settings)
                    self.panel.login(key)
                    self.panel.probe(key)
                    self.assertEqual(settings, self.store.read()["accounts"][key])

    def test_login_upgrades_placeholder_and_preserves_existing_identity(self):
        for platform, native in panel_module.NATIVE_PROVIDERS.items():
            with self.subTest(platform=platform):
                key = f"{platform}:fixture"
                self.store.update(key, self.placeholder)
                self.panel.login(key)
                self.assertEqual({**self.placeholder, "provider": native}, self.store.read()["accounts"][key])
        self.assertEqual(len(panel_module.NATIVE_PROVIDERS), self.commands.call_count)

    def test_probe_persists_upgrade_before_export_or_collection(self):
        for platform, native in panel_module.NATIVE_PROVIDERS.items():
            with self.subTest(platform=platform):
                key = f"{platform}:fixture"
                self.store.update(key, self.placeholder)
                observed = []
                def run(command, **kwargs):
                    observed.append((command[2], self.store.read()["accounts"][key]["provider"]))
                    return SimpleNamespace(returncode=0)
                self.commands.side_effect = run
                self.panel.probe(key)
                self.assertEqual([("export-session", native), ("probe", native)], observed)
                self.assertTrue(self.panel.jobs[key]["success"])

    def test_custom_channels_login_and_probe_do_not_bind_or_overwrite_recipe(self):
        key = "wechat_channels:fixture"
        settings = {**self.placeholder, "workflows": {"profile": {"url": "https://channels.weixin.qq.com/platform"}},
                    "profile": {"expected_id": "custom-id"}, "content_mapping": {"fields": {"id": "id"}}}
        settings.pop("expected_finder_id")
        self.store.update(key, settings)
        self.panel.login(key)
        self.assertEqual(settings, self.store.read()["accounts"][key])
        self.commands.reset_mock()
        self.panel.probe(key)
        self.assertEqual(["export-session", "probe"], [call.args[0][2] for call in self.commands.call_args_list])
        self.assertEqual(settings, self.store.read()["accounts"][key])

    def test_official_and_explicit_nonbrowser_providers_are_unchanged(self):
        for platform in ("wechat_service", "wechat_subscription"):
            key = f"{platform}:fixture"
            settings = {**self.placeholder, "provider": "wechat_official"}
            self.store.update(key, settings)
            self.panel.probe(key)
            self.assertEqual(settings, self.store.read()["accounts"][key])
        settings = {**self.placeholder, "provider": "custom-native"}
        self.assertEqual(settings, panel_module.onboarding_settings(self.accounts["bilibili:fixture"], settings))

    def test_summary_never_migrates_stored_placeholders(self):
        self.store.update("zhihu:fixture", self.placeholder)
        before = self.store.path.read_bytes()
        self.panel.summary()
        self.assertEqual(before, self.store.path.read_bytes())
        self.commands.assert_not_called()
