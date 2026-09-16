import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CliIsolationTests(unittest.TestCase):
    def test_missing_sessions_preserve_snapshots_and_fail_explicitly_without_network(self):
        with tempfile.TemporaryDirectory(prefix="promotion-cli-") as tmp:
            runtime = Path(tmp)
            (runtime / "data").mkdir()
            originals = {}
            for name in ("dashboard_data.json", "article_dashboard_data.json"):
                source = ROOT / "data" / name
                shutil.copyfile(source, runtime / "data" / name)
                originals[name] = json.loads(source.read_text())
            guard = runtime / "guard"
            guard.mkdir()
            (guard / "sitecustomize.py").write_text('''
import requests, smtplib
def denied(*a, **k):
    raise AssertionError("CLI fixture forbids network transport")
requests.sessions.Session.request = denied
smtplib.SMTP = smtplib.SMTP_SSL = denied
''')
            env = {**os.environ, "PROMOTION_RUNTIME_DIR": tmp, "PROMOTION_DATA_DIR": str(Path(tmp) / "data"), "PROMOTION_TEST_MODE": "1",
                   "PROMOTION_PROVIDER_CONFIG": str(runtime / "providers.json"),
                   "PROMOTION_SESSION_DIR": str(runtime / "sessions"), "PYTHONPATH": str(guard)}
            env.pop("TIKHUB_API_KEY", None)
            commands = [
                ["fetch_data.py", "--no-enrich-bili"],
                ["fetch_article_data.py", "--only", "zhihu", "--only", "xiaohongshu", "--only", "wechat_service", "--only", "wechat_subscription"],
                ["comment_monitor.py", "--platform", "bilibili", "--limit", "1", "--no-discovery"],
            ]
            for command in commands:
                result = subprocess.run([sys.executable, str(ROOT / "pipeline" / command[0]), *command[1:]],
                                        env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertNotIn("CLI fixture forbids network", result.stdout + result.stderr)
            for name, key in (("dashboard_data.json", "videos"), ("article_dashboard_data.json", "articles")):
                current = json.loads((runtime / "data" / name).read_text())
                self.assertEqual(len(originals[name][key]), len(current[key]))
                id_key = "video_id" if key == "videos" else "article_id"
                old = {(r["account_key"], r[id_key]): r["stats"] for r in originals[name][key]}
                new = {(r["account_key"], r[id_key]): r["stats"] for r in current[key]}
                self.assertEqual(old, new)
                self.assertEqual(0, current["api_calls"])
            self.assertTrue((runtime / "web" / "data" / "dashboard_data.json").exists())
            self.assertTrue((runtime / "web" / "articles" / "data" / "article_dashboard_data.json").exists())
            self.assertFalse((runtime / "data" / "comment_alert.json").exists())
            state = json.loads((runtime / "data" / "comment_state.json").read_text())
            self.assertEqual({}, state["seen_comments"])
