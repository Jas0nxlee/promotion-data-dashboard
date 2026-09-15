import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from providers.base import Pages, Collection, ProviderError, identifier, number
from providers.browser import BrowserSource, allowed, session_key, expand
from providers.mapped import MappedBrowserProvider, normalize_record
from providers.registry import ProviderRegistry, PLATFORMS
from providers.bilibili import BilibiliProvider
from providers.wechat_mp import WeChatOfficialProvider
from providers.http import Http
from providers.history import retain_known
import fetch_data
import fetch_article_data
import comment_monitor as cm
import send_comment_alerts as mail


ACCOUNT = {"platform": "bilibili", "account_name": "测试账号", "platform_uid": "123", "business_line": "望获"}
MAPPING = {"fields": {"id": "id", "title": "title", "published_at": "created", "url": "url"},
           "stats": {"play": "views"}, "definitions": {"play": "lifetime_views"}}
SETTINGS = {"profile": {"expected_id": "123", "id_path": "uid", "name_path": "name"},
            "content_mapping": MAPPING,
            "comment_identity_compatible": True,
            "comment_mapping": {"id": "id", "content": "text", "reply_count": "count", "created_at": "time"}}


class Source:
    call_count = 0

    def __init__(self, responses):
        self.responses = responses

    @contextmanager
    def session(self):
        yield self

    def pages(self, name, *a, **kw):
        self.call_count += 1
        result = self.responses[name]
        if isinstance(result, Exception):
            raise result
        return result


class ProviderTests(unittest.TestCase):
    def test_identifier_preserves_large_integer_and_rejects_float(self):
        self.assertEqual("12345678901234567890", identifier(12345678901234567890))
        with self.assertRaises(ProviderError):
            identifier(float(12345678901234567890))

    def test_abbreviated_or_invalid_metrics_are_unknown(self):
        for value in ("1.2万", "--", float("nan"), -1, True):
            self.assertIsNone(number(value))
        self.assertEqual(0, number("0"))

    def test_unbound_account_fails_without_network_or_paid_fallback(self):
        with patch("requests.Session.request") as send:
            with self.assertRaisesRegex(ProviderError, "setup_required"):
                ProviderRegistry({"accounts": {}}).get(ACCOUNT)
        send.assert_not_called()

    def test_host_guard_rejects_paid_and_cross_platform_hosts(self):
        http = Http({"api.bilibili.com"}, session=Mock())
        for url in ("https://api.tikhub.io/test", "https://api.bilibili.com.evil.test/x", "http://api.bilibili.com/x"):
            with self.assertRaisesRegex(ProviderError, "invalid_host"):
                http.request("GET", url)
        http.session.request.assert_not_called()
        self.assertFalse(allowed("https://bilibili.com.evil.test", "bilibili"))

    def test_full_content_url_and_encoded_identifiers(self):
        url = "https://www.bilibili.com/video/BVtest"
        self.assertEqual(url, expand("{content_url}", {"content_url": url}))
        self.assertEqual("https://www.bilibili.com/?id=a%26b", expand("https://www.bilibili.com/?id={id}", {"id": "a&b"}))

    def test_rate_limit_does_not_keep_retrying(self):
        session = Mock()
        session.request.return_value.status_code = 429
        with self.assertRaisesRegex(ProviderError, "rate_limited"):
            Http({"api.bilibili.com"}, interval=0, session=session).request("GET", "https://api.bilibili.com/x")
        self.assertEqual(1, session.request.call_count)

    def test_identity_mismatch_prevents_content_collection(self):
        source = Source({"profile": Pages([{"uid": "someone_else"}], 1, True)})
        with self.assertRaisesRegex(ProviderError, "identity_mismatch"):
            MappedBrowserProvider(ACCOUNT, SETTINGS, source).collect()
        self.assertEqual(1, source.call_count)

    def test_all_browser_platforms_normalize_and_mark_partial(self):
        for platform in PLATFORMS:
            with self.subTest(platform=platform):
                account = {**ACCOUNT, "platform": platform}
                source = Source({"profile": Pages([{"uid": "123"}], 1, True),
                                 "contents": Pages([{"id": "90071992547409999", "title": "内容", "created": 1720000000}], 1, False, "page limit")})
                result = MappedBrowserProvider(account, SETTINGS, source).collect()
                self.assertFalse(result.complete)
                self.assertEqual("page limit", result.note)
                self.assertEqual(1, len(result.records))

    def test_unknown_metric_definition_is_not_silently_published(self):
        mapping = {**MAPPING, "definitions": {}}
        with self.assertRaisesRegex(ProviderError, "统计口径"):
            normalize_record({"id": "1", "views": 50}, mapping, "bilibili", "video")

    def test_comments_have_parent_relation_and_full_reply_count(self):
        source = Source({"profile": Pages([{"uid": "123"}], 1, True),
                         "comments": Pages([{"id": "root", "count": 1}], 2, True),
                         "replies": Pages([{"id": "child", "text": "new", "time": 1788220860}], 1, True)})
        provider = MappedBrowserProvider(ACCOUNT, SETTINGS, source)
        comments, stats = provider.comments({"content_id": "video"})
        self.assertEqual("root", comments[1]["parent_comment_id"])
        self.assertEqual(2, stats["root_pages"])
        self.assertEqual(1, stats["reply_pages"])

    def test_missing_reply_count_is_not_complete(self):
        source = Source({"profile": Pages([{"uid": "123"}], 1, True),
                         "comments": Pages([{"id": "root"}], 1, True)})
        with self.assertRaisesRegex(ProviderError, "incomplete_replies"):
            MappedBrowserProvider(ACCOUNT, SETTINGS, source).comments({"content_id": "video"})

    def test_migration_gate_prevents_state_advance(self):
        settings = {**SETTINGS, "comment_identity_compatible": False}
        provider = MappedBrowserProvider(ACCOUNT, settings, Source({}))
        registry = Mock()
        registry.fetch_comments.side_effect = lambda *a: provider.comments(a[1])
        item = {**ACCOUNT, "account_key": "bilibili:测试账号", "content_id": "v", "platform_label": "B站"}
        state = {"baseline_done": True, "monitor_started_at": "2026-09-01T00:00:00+08:00", "seen_comments": {"bilibili:v": ["old"]}}
        args = SimpleNamespace(platform=[], limit=0, max_pages=10, no_replies=False)
        with patch.object(cm, "load_state", return_value=state):
            items, errors, updated = cm.check_comments(registry, [item], args)
        self.assertFalse(items)
        self.assertEqual(1, len(errors))
        self.assertEqual(["old"], updated["seen_comments"]["bilibili:v"])

    def test_test_mode_cannot_connect_smtp(self):
        with patch.dict(os.environ, {"PROMOTION_TEST_MODE": "1"}), patch("smtplib.SMTP") as smtp:
            with self.assertRaisesRegex(RuntimeError, "禁止连接"):
                mail.connect({})
        smtp.assert_not_called()

    def test_bilibili_detail_keeps_source_and_unknown_download(self):
        http = Mock(call_count=1)
        http.request.return_value = {"code": 0, "data": {"bvid": "BVtest", "aid": 12345678901234567890, "stat": {"view": 42}, "owner": {"mid": 123}}}
        record = BilibiliProvider(ACCOUNT, {}, http=http).video_detail("BVtest")
        self.assertEqual(42, record["stats"]["play"])
        self.assertIsNone(record["stats"]["download"])
        self.assertEqual("12345678901234567890", record["aid"])

    def test_public_enrichment_does_not_replace_creator_statistics(self):
        collection = Collection({}, [{"video_id": "BVtest", "stats": {"play": 100, "like": None}}])
        p = BilibiliProvider(ACCOUNT, {})
        detail = {"stats": {"play": 42, "like": 2}, "metric_provenance": {"like": {"source": "bilibili_public"}}}
        with patch.object(MappedBrowserProvider, "collect", return_value=collection), patch.object(p, "video_detail", return_value=detail):
            result = p.collect()
        self.assertEqual(100, result.records[0]["stats"]["play"])
        self.assertEqual(2, result.records[0]["stats"]["like"])

    def test_unconfigured_video_preserves_last_good_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            config, old = Path(folder) / "accounts.json", Path(folder) / "old.json"
            config.write_text(json.dumps({"accounts": [ACCOUNT]}))
            key = "bilibili:测试账号"
            row = {"video_id": "BVtest", "platform": "bilibili", "account_key": key, "stats": {"play": 42}}
            old.write_text(json.dumps({"updated_at": "2026-09-01T00:00:00+08:00", "accounts": [{**ACCOUNT, "account_key": key}], "videos": [row]}))
            args = SimpleNamespace(out=old, no_enrich_bili=True)
            with patch.object(fetch_data, "CONFIG_PATH", config):
                result = fetch_data.collect(args, ProviderRegistry({"accounts": {}}))
            self.assertEqual("stale", result["accounts"][0]["status"])
            self.assertEqual(42, result["videos"][0]["stats"]["play"])
            self.assertEqual(0, result["api_calls"])

    def test_article_no_publish_option_writes_only_requested_file(self):
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "out.json"
            with patch.object(fetch_article_data, "WEB_JSON_PATH", Path(folder) / "web.json"):
                fetch_article_data.write_outputs({"test": True}, dest, publish_web=False)
            self.assertTrue(dest.exists())
            self.assertFalse((Path(folder) / "web.json").exists())

    def test_missing_current_fields_retain_historical_provenance(self):
        old = {"article_id": "1", "published_at": "2020-01-01T00:00:00+08:00", "stats": {"read": 42}}
        current = {"article_id": "1", "published_at": None, "stats": {"read": None}}
        result = retain_known([current], [old], "article_id")[0]
        self.assertEqual(42, result["stats"]["read"])
        self.assertEqual(old["published_at"], result["published_at"])
        self.assertEqual("cached", result["metric_provenance"]["read"]["source"])
        self.assertIsNone(current["stats"]["read"])

    def test_explicit_zero_is_not_replaced_with_old_value(self):
        rows = retain_known([{"video_id": "1", "stats": {"play": 0}}],
                            [{"video_id": "1", "stats": {"play": 99}}], "video_id")
        self.assertEqual(0, rows[0]["stats"]["play"])


class WeChatTests(unittest.TestCase):
    def provider(self):
        account = {**ACCOUNT, "platform": "wechat_service"}
        config = {"provider": "wechat_official", "bound_account_key": "wechat_service:测试账号", "access_token_env": "TEST_MP_TOKEN"}
        return WeChatOfficialProvider(account, config, Mock(call_count=1))

    def test_published_parts_keep_legacy_ids_and_unknown_metrics(self):
        p = self.provider()
        p.http.request.return_value = {"total_count": 1, "item": [{"article_id": "opaque", "update_time": 1788220860,
            "content": {"news_item": [{"title": "文章", "url": "https://mp.weixin.qq.com/s?mid=100&idx=2"}]}}]}
        with patch.dict(os.environ, {"TEST_MP_TOKEN": "test-not-a-real-token"}):
            result = p.collect()
        self.assertEqual("100-2", result.records[0]["article_id"])
        self.assertIsNone(result.records[0]["published_at"])
        self.assertIsNone(result.records[0]["stats"]["read"])
        self.assertFalse(result.complete)

    def test_permission_error_does_not_fall_back_to_paid_provider(self):
        p = self.provider()
        p.http.request.return_value = {"errcode": 48001}
        with patch.dict(os.environ, {"TEST_MP_TOKEN": "test-not-a-real-token"}):
            with self.assertRaisesRegex(ProviderError, "permission_denied"):
                p.collect()
        self.assertEqual(1, p.http.request.call_count)

    def test_daily_readers_are_not_cumulative_views(self):
        p = self.provider()
        p.http.request.return_value = {"list": [{"msgid": "100_2", "detail": {"read_user": 17}}], "is_delay": "true"}
        with patch.dict(os.environ, {"TEST_MP_TOKEN": "test-not-a-real-token"}):
            result = p.daily_readers("2026-09-01")
        self.assertEqual("daily_readers", result["metric"])
        self.assertTrue(result["delayed"])
        self.assertEqual(17, result["items"][0]["readers"])
        self.assertNotIn("read", result["items"][0])

    def test_unsupported_date_is_rejected_before_network(self):
        p = self.provider()
        with self.assertRaisesRegex(ProviderError, "invalid_date"):
            p.daily_readers("2024-01-01")
        p.http.request.assert_not_called()
