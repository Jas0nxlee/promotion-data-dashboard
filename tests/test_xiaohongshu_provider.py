import copy
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from providers.base import Pages, ProviderError
from providers.xiaohongshu import XiaohongshuProvider, note_record

ACCOUNT = {"platform": "xiaohongshu", "account_name": "测试", "provided_id": "123456789", "platform_uid": "a" * 24}
NOTE = {"id": "b" * 24, "display_title": "示例笔记", "time": "2026-09-15 13:16",
        "likes": 2, "view_count": 100, "collected_count": 4, "comments_count": 3, "shared_count": 1,
        "xsec_token": "private-navigation-value", "type": "normal", "tab_status": 1}


class Source:
    call_count = 2

    def __init__(self):
        self.profile = {"red_num": "123456789", "name": "测试", "fans_count": 8}
        self.result = Pages([copy.deepcopy(NOTE)], 1, True, envelopes=[{
            "data": {"tags": [{"checked": True, "notes_count": 1}]}}])

    @contextmanager
    def session(self):
        yield self

    def pages(self, name, **kwargs):
        return Pages([self.profile], 1, True) if name == "profile" else self.result


class XiaohongshuTests(unittest.TestCase):
    def test_lifetime_fields_and_navigation_tokens_do_not_leak(self):
        source = Source()
        result = XiaohongshuProvider(ACCOUNT, {}, source).collect()
        self.assertTrue(result.complete)
        row = result.records[0]
        self.assertEqual({"read": 100, "like": 2, "comment": 3, "share": 1, "collect": 4}, row["stats"])
        self.assertEqual("2026-09-15T13:16:00+08:00", row["published_at"])
        self.assertNotIn("private-navigation-value", str(result))

    def test_account_number_mismatch_stops_collection(self):
        source = Source()
        source.profile["red_num"] = "different"
        with self.assertRaisesRegex(ProviderError, "identity_mismatch"):
            XiaohongshuProvider(ACCOUNT, {}, source).collect()

    def test_pinned_duplicate_deduplicated_and_total_only_on_first_page(self):
        source = Source()
        source.result.rows.append(copy.deepcopy(NOTE))
        source.result.envelopes.append({"data": {"tags": []}})
        result = XiaohongshuProvider(ACCOUNT, {}, source).collect()
        self.assertTrue(result.complete)
        self.assertEqual(1, len(result.records))

    def test_total_changes_or_count_mismatch_cannot_claim_complete(self):
        source = Source()
        source.result.envelopes.append({"data": {"tags": [{"checked": True, "notes_count": 2}]}})
        with self.assertRaisesRegex(ProviderError, "incomplete_pagination"):
            XiaohongshuProvider(ACCOUNT, {}, source).collect()
        source.result.envelopes.pop(0)
        self.assertFalse(XiaohongshuProvider(ACCOUNT, {}, source).collect().complete)

    def test_invalid_id_rejected_and_negative_metrics_unknown(self):
        with self.assertRaisesRegex(ProviderError, "稳定"):
            note_record({**NOTE, "id": ""})
        self.assertIsNone(note_record({**NOTE, "view_count": -1})["stats"]["read"])

