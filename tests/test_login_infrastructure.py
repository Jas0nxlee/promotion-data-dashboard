"""Private credential initialization, gateway policy, and Compose isolation."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


init = load("promotion_init_authorization", ROOT / "scripts/init_authorization.py")
gateway = load("promotion_login_configure", ROOT / "docker/login/configure.py")
HASH = "operator:$2y$12$" + "a" * 53 + "\n"


class LoginInfrastructureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="promotion-login-tests-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_bcrypt_password_uses_stdin_not_command_arguments(self):
        secret = "fixture-only-password-123"
        with patch.object(init.shutil, "which", return_value="/usr/bin/htpasswd"), \
             patch.object(init.subprocess, "run", return_value=Mock(returncode=0, stdout=HASH)) as run:
            self.assertEqual(HASH, init.password_hash("operator", secret))
        arguments = run.call_args.args[0]
        self.assertNotIn(secret, arguments)
        self.assertIn("-i", arguments)
        self.assertIn("-B", arguments)
        self.assertNotIn("-b", arguments)
        self.assertEqual(secret + "\n", run.call_args.kwargs["input"])

    def test_explicit_migration_keeps_source_and_creates_private_files(self):
        old = self.folder / "legacy.json"
        contents = json.dumps({"accounts": {"zhihu:fixture": {"provider": "browser"}}})
        old.write_text(contents)
        with patch.object(init, "password_hash", return_value=HASH):
            auth, config = init.initialize(self.folder / "auth", self.folder / "providers", self.folder / "sessions",
                                           "operator", "fixture-only-password-123", migrate=old)
        self.assertEqual(contents, old.read_text())
        migrated = json.loads(config.read_text())["accounts"]["zhihu:fixture"]
        self.assertEqual({"provider": "browser", "session_mode": "portable", "channel": "chromium"}, migrated)
        self.assertEqual(HASH, auth.read_text())
        self.assertEqual(0o600, auth.stat().st_mode & 0o777)
        self.assertEqual(0o600, config.stat().st_mode & 0o777)
        self.assertTrue((self.folder / "sessions/authorization").is_dir())
        with self.assertRaises(FileExistsError), patch.object(init, "password_hash") as hashed:
            init.initialize(auth.parent, config.parent, self.folder / "sessions", "operator", "different-test-password")
        hashed.assert_not_called()

    def test_migration_converts_only_browser_sessions_and_preserves_bindings_and_recipes(self):
        settings = {"provider": "browser", "cdp_url": "http://127.0.0.1:61234", "headed": True,
                    "session_mode": "persistent", "channel": "chrome", "expected_uid": "123",
                    "content_aliases": {"native": "legacy"}, "workflows": {"contents": {"url": "https://example.invalid/"}}}
        official = {"provider": "wechat_official", "bound_platform_uid": "gh_fixture", "app_id_env": "APP_ID"}
        old = self.folder / "legacy.json"
        original = json.dumps({"schema_version": 1, "accounts": {"browser": settings, "default": {"expected_uid": "456"}, "official": official}})
        old.write_text(original)
        with patch.object(init, "password_hash", return_value=HASH):
            _, config = init.initialize(self.folder / "auth", self.folder / "providers", self.folder / "sessions",
                                        "operator", "fixture-only-password-123", migrate=old)
        result = json.loads(config.read_text())["accounts"]
        self.assertEqual(original, old.read_text())
        self.assertEqual(official, result["official"])
        for key in ("browser", "default"):
            self.assertEqual("portable", result[key]["session_mode"])
            self.assertEqual("chromium", result[key]["channel"])
            self.assertNotIn("cdp_url", result[key]); self.assertNotIn("headed", result[key])
        self.assertEqual(settings["expected_uid"], result["browser"]["expected_uid"])
        self.assertEqual(settings["content_aliases"], result["browser"]["content_aliases"])
        self.assertEqual(settings["workflows"], result["browser"]["workflows"])

    def test_existing_provider_and_symlinks_cannot_be_overwritten(self):
        provider = self.folder / "providers/providers.json"
        provider.parent.mkdir(); provider.write_text('{"accounts":{"existing":{}}}')
        old = self.folder / "old.json"; old.write_text('{"accounts":{}}')
        with self.assertRaises(FileExistsError):
            init.initialize(self.folder / "auth", provider.parent, self.folder / "sessions", "operator", "fixture-only-password-123", migrate=old)
        self.assertIn("existing", provider.read_text())
        link = self.folder / "linked"; link.symlink_to(old)
        with self.assertRaises(ValueError):
            init.atomic_private(link, "replacement", replace=True)
        self.assertEqual('{"accounts":{}}', old.read_text())

    def test_noninteractive_default_never_generates_or_prints_password(self):
        output, error = io.StringIO(), io.StringIO()
        with patch.object(init.sys, "stdin", io.StringIO()), patch.object(init.sys, "stdout", output), \
             patch.object(init.sys, "stderr", error), patch.object(init.secrets, "token_urlsafe") as generate:
            with self.assertRaises(SystemExit):
                init.main(["--provider-dir", str(self.folder / "providers")])
        generate.assert_not_called()
        self.assertEqual("", output.getvalue())

    def test_password_stdin_output_contains_only_paths_and_username(self):
        output, secret = io.StringIO(), "fixture-only-password-123"
        with patch.object(init.sys, "stdin", io.StringIO(secret)), patch.object(init.sys, "stdout", output), \
             patch.object(init, "password_hash", return_value=HASH):
            init.main(["--password-stdin", "--auth-dir", str(self.folder / "auth"),
                       "--provider-dir", str(self.folder / "providers"), "--sessions-dir", str(self.folder / "sessions")])
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn(HASH.strip(), output.getvalue())

    def test_gateway_fails_closed_without_private_bcrypt_credentials(self):
        auth = self.folder / "admin.htpasswd"
        template = (ROOT / "docker/login/nginx.conf.template").read_text()
        with self.assertRaises(ValueError):
            gateway.render(template, "http://127.0.0.1:18762", auth)
        auth.write_text("operator:plaintext\n"); auth.chmod(0o600)
        with self.assertRaises(ValueError):
            gateway.render(template, "http://127.0.0.1:18762", auth)
        auth.write_text(HASH); auth.chmod(0o644)
        with self.assertRaises(ValueError):
            gateway.render(template, "http://127.0.0.1:18762", auth)
        auth.chmod(0o600)
        config = gateway.render(template, "http://127.0.0.1:18762", auth)
        self.assertNotIn("@@", config)
        self.assertIn('auth_basic_user_file "' + str(auth), config)
        self.assertNotIn(HASH.strip(), config)

    def test_gateway_origin_and_proxy_contract(self):
        for origin in ("http://user:password@localhost:18762", "http://localhost:18762/path", "http://localhost:18762/",
                       "http://localhost:18762?x=1", "http://localhost:99999", 'http://localhost\";auth_basic off;'):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                gateway.validate_origin(origin)
        template = (ROOT / "docker/login/nginx.conf.template").read_text()
        self.assertIn('proxy_set_header Host $http_host;', template)
        self.assertIn('proxy_set_header Origin $http_origin;', template)
        self.assertIn('if ($desktop_origin_allowed = 0) { return 403; }', template)
        self.assertIn('proxy_set_header Upgrade $http_upgrade;', template)
        self.assertIn('proxy_set_header Authorization "";', template)
        self.assertNotIn('auth_basic off', template)

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose not installed")
    def test_compose_only_publishes_gateway_and_mounts_provider_directory(self):
        for filename, base_service in (("docker-compose.yml", "scheduler"), ("docker-compose.local.yml", "collector")):
            with self.subTest(filename=filename):
                # Explicit empty env-file and no env resolution avoid reading project secrets.
                result = subprocess.run(["docker", "compose", "--env-file", os.devnull, "-f", str(ROOT / filename),
                                         "--profile", "login", "config", "--no-env-resolution", "--format", "json"],
                                        env={**os.environ, "PROMOTION_DEV_ID": "promotion-login-test", "DATA_DAILY_REQUEST_LIMIT": "37"},
                                        capture_output=True, text=True, check=True)
                services = json.loads(result.stdout)["services"]
                login = services["login"]
                self.assertEqual([18762], [port["target"] for port in login["ports"]])
                self.assertEqual("127.0.0.1", login["ports"][0]["host_ip"])
                self.assertNotIn("depends_on", login)
                provider_mount = next(v for v in login["volumes"] if v["target"] == "/run/promotion/providers")
                self.assertFalse(provider_mount.get("read_only", False))
                self.assertFalse(provider_mount["bind"]["create_host_path"])
                self.assertFalse(provider_mount["source"].endswith("providers.json"))
                base_mounts = [v for v in services[base_service]["volumes"] if v["target"] in ("/run/promotion/providers", "/config-private")]
                self.assertEqual(1, len(base_mounts)); self.assertTrue(base_mounts[0]["read_only"])
                self.assertEqual("/app/.runtime", login["environment"]["PROMOTION_RUNTIME_DIR"])
                self.assertEqual("/app/data", login["environment"]["PROMOTION_DATA_DIR"])
                self.assertEqual("37", login["environment"]["DATA_DAILY_REQUEST_LIMIT"])
                self.assertEqual("37", services[base_service]["environment"]["DATA_DAILY_REQUEST_LIMIT"])
                self.assertEqual(["python", "pipeline/provider_setup.py", "status"], services[base_service]["command"]) if base_service == "collector" else None
