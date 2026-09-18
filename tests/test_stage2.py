import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import copy
from concurrent.futures import Future
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from providers.base import Collection, ProviderError
from providers.credentials import WeChatToken
from providers.identity import reconcile_contents, validate_aliases, native_id
from providers.exports import read_export
from providers.health import record_verification, read_verification, record_comment_verification
from providers.settings import SettingsStore
from providers.bilibili_creator import BilibiliCreatorProvider
from providers.browser import BrowserSource
import scheduler


class TokenTests(unittest.TestCase):
    def test_reuses_cache_and_renews_near_expiry_without_forcing_other_tokens_invalid(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"WX_APP": "app", "WX_SECRET": "fake-secret"}):
            http = Mock()
            http.request.return_value = {"access_token": "test-token", "expires_in": 7200}
            clock = [1000]
            config = {"app_id_env": "WX_APP", "app_secret_env": "WX_SECRET", "expected_app_id": "app"}
            token = WeChatToken(config, http, Path(tmp), lambda: clock[0])
            self.assertEqual("test-token", token.get())
            self.assertEqual("test-token", token.get())
            self.assertEqual(1, http.request.call_count)
            clock[0] += 7000
            token.get()
            self.assertEqual(2, http.request.call_count)
            self.assertFalse(http.request.call_args.kwargs["json"]["force_refresh"])
            cache = next(Path(tmp).glob("*.json"))
            self.assertEqual(0o600, cache.stat().st_mode & 0o777)

    def test_wrong_app_id_is_rejected_before_credentials_leave_process(self):
        with patch.dict(os.environ, {"WX_APP": "wrong", "WX_SECRET": "fake"}):
            http = Mock()
            with self.assertRaisesRegex(ProviderError, "identity_mismatch"):
                WeChatToken({"app_id_env": "WX_APP", "app_secret_env": "WX_SECRET", "expected_app_id": "right"}, http).get()
            http.request.assert_not_called()

    def test_portable_session_export_excludes_other_platforms(self):
        with tempfile.TemporaryDirectory() as tmp, patch("providers.browser.SESSIONS", Path(tmp)):
            source = BrowserSource({"platform": "bilibili", "account_name": "测试"}, {})
            source.context = Mock()
            source.context.storage_state.return_value = {
                "cookies": [{"domain": ".bilibili.com", "name": "test", "value": "fake"},
                            {"domain": ".unrelated.test", "name": "other", "value": "fake"}],
                "origins": [{"origin": "https://member.bilibili.com", "localStorage": []},
                            {"origin": "https://unrelated.test", "localStorage": []}]}
            path = source.export_session()
            stored = json.loads(path.read_text())
            self.assertEqual(1, len(stored["cookies"]))
            self.assertEqual(1, len(stored["origins"]))
            self.assertEqual(0o600, path.stat().st_mode & 0o777)


class MigrationTests(unittest.TestCase):
    def test_matches_stable_url_and_does_not_match_same_title(self):
        before = [{"article_id": "old", "title": "同名", "url": "https://mp.weixin.qq.com/s?__biz=a&mid=10&idx=1&scene=3"}]
        after = [{"article_id": "new", "title": "不同标题", "url": "https://mp.weixin.qq.com/s?mid=10&idx=1&__biz=a&scene=5"},
                 {"article_id": "other", "title": "同名", "url": "https://mp.weixin.qq.com/s?mid=11&idx=1&__biz=a"}]
        result = reconcile_contents(before, after, "article_id")
        self.assertEqual({"new": "old"}, result["content_aliases"])
        self.assertEqual(["other"], result["unresolved_or_new"])
        self.assertEqual("new", native_id("old", result["content_aliases"]))

    def test_mapping_collisions_and_cycles_fail(self):
        for aliases in ({"a": "b", "b": "a"}, {"a": "c", "b": "c"}):
            with self.assertRaises(ProviderError):
                validate_aliases(aliases)


class ExportTests(unittest.TestCase):
    def test_csv_preserves_long_ids_and_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.csv"
            path.write_text("ID,播放\n12345678901234567890,0\n", encoding="utf-8-sig")
            rows = read_export(path)
            self.assertEqual("12345678901234567890", rows[0]["ID"])
            self.assertEqual("0", rows[0]["播放"])

    def test_duplicate_headers_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.csv"
            path.write_text("id,id\n1,2\n")
            with self.assertRaisesRegex(ProviderError, "表头"):
                read_export(path)

    def test_xlsx_requires_explicit_sheet_and_reads_values(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.xlsx"
            book = openpyxl.Workbook()
            book.active.title = "数据"
            book.active.append(["ID", "播放"])
            book.active.append(["12345678901234567890", 42])
            book.create_sheet("说明")
            book.save(path)
            with self.assertRaisesRegex(ProviderError, "多工作表"):
                read_export(path)
            self.assertEqual(42, read_export(path, sheet="数据")[0]["播放"])


class HealthTests(unittest.TestCase):
    def test_ready_requires_real_identity_contents_and_reply_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            config = {"provider": "bilibili_creator"}
            result = Collection({"verified_account_id": "1"}, [{"video_id": "v", "stats": {"like": 1, "comment": 1}}], True)
            record_verification("bilibili:测试", config, result, directory=directory)
            self.assertFalse(read_verification("bilibili:测试", config, directory)["ready"])
            record_comment_verification("bilibili:测试", config, [{"parent_comment_id": "1"}], directory)
            self.assertTrue(read_verification("bilibili:测试", config, directory)["ready"])
            changed = {**config, "expected_uid": "different"}
            self.assertFalse(read_verification("bilibili:测试", changed, directory)["ready"])

    def test_inline_credentials_rejected_and_existing_account_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SettingsStore(Path(tmp) / "providers.json")
            store.update("a", {"provider": "browser"})
            store.update("b", {"access_token_env": "MY_TOKEN"})
            with self.assertRaisesRegex(ProviderError, "明文凭证"):
                store.update("a", {"access_token": "fake"})
            self.assertEqual({"a", "b"}, set(store.read()["accounts"]))


class NativeBrowser:
    def __init__(self, replies):
        self.responses = list(replies)
        self.call_count = 0
        self.calls = []

    @contextmanager
    def session(self):
        yield self

    def get_json(self, url, params, *, min_interval=None):
        self.call_count += 1
        self.calls.append((url, params, min_interval))
        return {"code": 0, "data": self.responses.pop(0)}


class BilibiliNativeTests(unittest.TestCase):
    account = {"platform": "bilibili", "account_name": "测试", "platform_uid": "123"}
    profile = {"isLogin": True, "mid": 123, "uname": "测试"}

    def test_creator_list_pagination_uses_native_archive_shape(self):
        def row(bvid):
            return {"Archive": {"bvid": bvid, "aid": 123, "mid": 123, "ptime": 1789125399},
                    "stat": {"view": 50, "reply": 2}, "cid_list": [12345678901234567890]}
        source = NativeBrowser([self.profile, {"mid": 123, "follower": 10},
            {"arc_audits": [row("BV1")], "page": {"count": 2}},
            {"arc_audits": [row("BV2")], "page": {"count": 2}}])
        result = BilibiliCreatorProvider(self.account, {}, source).collect()
        self.assertTrue(result.complete)
        self.assertEqual(2, len(result.records))
        self.assertEqual("12345678901234567890", result.records[0]["cid"])
        self.assertEqual(2, source.calls[-1][1]["pn"])

    def test_comments_and_replies_share_creator_inbox_scan(self):
        root = {"rpid": 10, "root": 0, "bvid": "BV1", "member": {"mid": 456}, "rcount": 1, "content": {"message": "问"}}
        reply = {"rpid": 11, "root": 10, "bvid": "BV1", "member": {"mid": 123}, "rcount": 0, "content": {"message": "答"}}
        source = NativeBrowser([self.profile, {"page": {"total": 2}, "list": [root]}, {"page": {"total": 2}, "list": [reply]}])
        provider = BilibiliCreatorProvider(self.account, {}, source)
        roots, stats = provider.comments({"content_id": "BV1"}, include_replies=False)
        replies, pages = provider.replies({"content_id": "BV1"}, "10")
        self.assertEqual(1, len(roots))
        self.assertEqual("10", replies[0]["parent_comment_id"])
        self.assertEqual(["123"], replies[0]["user_ids"])
        self.assertEqual(0, pages)
        self.assertEqual(3, source.call_count)
        self.assertEqual([None, 2.0, 2.0], [call[2] for call in source.calls])

    def test_wrong_logged_in_uid_stops_before_reading_content(self):
        source = NativeBrowser([{**self.profile, "mid": 999}])
        with self.assertRaisesRegex(ProviderError, "identity_mismatch"):
            BilibiliCreatorProvider(self.account, {}, source).collect()
        self.assertEqual(1, source.call_count)

    def test_comment_cap_is_not_treated_as_complete(self):
        source = NativeBrowser([self.profile, {"page": {"total": 50000}, "list": []}])
        with self.assertRaisesRegex(ProviderError, "coverage_limited"):
            BilibiliCreatorProvider(self.account, {}, source).comments({"content_id": "BV1"})


class PendingExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, function):
        future = Future()
        self.calls.append((function.__name__, future))
        return future


class DispatchTests(unittest.TestCase):
    def test_slow_daily_lane_does_not_block_hourly_comments(self):
        executor = PendingExecutor()
        engine = scheduler.DispatchScheduler(executor)
        state = [{}]
        now = datetime(2026, 9, 15, 10, 23, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as tmp, patch.object(scheduler, "DATA", Path(tmp)), \
             patch.object(scheduler, "load_state", side_effect=lambda: copy.deepcopy(state[0])), \
             patch.object(scheduler, "save_state", side_effect=lambda value: state.__setitem__(0, copy.deepcopy(value))):
            engine.tick(now)
            self.assertEqual(["run_comment_cycle", "run_data_collection"], [x[0] for x in executor.calls])
            executor.calls[0][1].set_result({"monitor": 0, "mail": 0})
            engine.tick(now + timedelta(hours=1))
            self.assertEqual(3, len(executor.calls))
            self.assertEqual("run_comment_cycle", executor.calls[2][0])
            self.assertEqual("2026-09-15T10", state[0]["last_comment_slot"])

    def test_failed_daily_job_waits_then_retries_same_day(self):
        executor = PendingExecutor()
        engine = scheduler.DispatchScheduler(executor)
        state = [{}]
        now = datetime(2026, 9, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as tmp, patch.object(scheduler, "DATA", Path(tmp)), \
             patch.object(scheduler, "load_state", side_effect=lambda: copy.deepcopy(state[0])), \
             patch.object(scheduler, "save_state", side_effect=lambda value: state.__setitem__(0, copy.deepcopy(value))):
            engine.tick(now)
            executor.calls[1][1].set_result({"video": 2})
            engine.tick(now + timedelta(minutes=1))
            engine.tick(now + timedelta(minutes=10))
            self.assertEqual(2, len(executor.calls))
            engine.tick(now + timedelta(minutes=17))
            self.assertEqual("run_data_collection", executor.calls[-1][0])
            self.assertNotIn("last_data_slot", state[0])


class PanelTests(unittest.TestCase):
    def test_cross_origin_control_requests_are_rejected(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        from control_panel import Panel, handler
        panel = Panel()
        panel.login = Mock()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(panel))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
            conn.request("POST", "/api/login", body=json.dumps({"account": "bilibili:望获OS"}),
                         headers={"Origin": "https://untrusted.example", "X-CSRF-Token": panel.token})
            response = conn.getresponse()
            self.assertEqual(403, response.status)
            response.read()
            panel.login.assert_not_called()
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            panel.executor.shutdown(wait=False)
