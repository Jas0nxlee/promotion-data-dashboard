import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from api_budget import ApiBudget, ApiBudgetExceeded


CN_TZ = timezone(timedelta(hours=8))


class ApiBudgetTests(unittest.TestCase):
    def test_default_unlimited_preserves_existing_usage_and_continues_past_800(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            path=Path(tmp)/'usage.json';now=datetime(2026,9,17,12,tzinfo=CN_TZ)
            path.write_text(json.dumps({'days':{'2026-09-17':{'date':'2026-09-17','used':1195,'limit':800,'blocked_attempts':2,'by_task':{},'by_endpoint':{}}}}))
            budget=ApiBudget(path=path,now_fn=lambda:now)
            day=budget.consume('next-authorized-read')
            self.assertEqual(1196,day['used']);self.assertEqual(0,day['limit'])
            self.assertIsNone(budget.remaining());self.assertEqual(2,budget.snapshot()['blocked_attempts'])

    def test_explicit_zero_disables_old_temporary_caps(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DATA_DAILY_REQUEST_LIMIT':'0'}):
            path=Path(tmp)/'usage.json';path.with_name('collection_budget_overrides.json').write_text(json.dumps({'2026-09-17':1}))
            budget=ApiBudget(path=path,now_fn=lambda:datetime(2026,9,17,tzinfo=CN_TZ))
            for _ in range(3):budget.consume('read')
            self.assertEqual(0,budget.snapshot()['limit']);self.assertIsNone(budget.remaining())

    def test_dated_override_is_shared_bounded_and_expires_without_resetting_usage(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DATA_DAILY_REQUEST_LIMIT':'2'}):
            path=Path(tmp)/'usage.json';override=path.with_name('collection_budget_overrides.json')
            clock=[datetime(2026,9,17,12,tzinfo=CN_TZ)]
            a=ApiBudget(path=path,now_fn=lambda:clock[0]);b=ApiBudget(path=path,now_fn=lambda:clock[0])
            a.consume('one');b.consume('two')
            override.write_text(json.dumps({'2026-09-17':3}))
            a.consume('approved-third')
            with self.assertRaises(ApiBudgetExceeded):b.consume('blocked-fourth')
            override.unlink()
            with self.assertRaises(ApiBudgetExceeded):a.consume('still-blocked-after-restore')
            self.assertEqual(3,a.snapshot()['used']);self.assertEqual(2,a.snapshot()['limit'])
            override.write_text(json.dumps({'2026-09-17':3}))
            clock[0]+=timedelta(days=1)
            self.assertEqual(2,b.snapshot()['limit']);self.assertEqual(0,b.snapshot()['used'])

    def test_invalid_dated_override_cannot_disable_budget(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DATA_DAILY_REQUEST_LIMIT':'800'}):
            path=Path(tmp)/'usage.json';override=path.with_name('collection_budget_overrides.json')
            for value in [0,-1,True,'1200']:
                override.write_text(json.dumps({'2026-09-17':value}))
                budget=ApiBudget(path=path,now_fn=lambda:datetime(2026,9,17,tzinfo=CN_TZ))
                with self.assertRaises(ValueError):budget.consume('blocked')
                self.assertFalse(path.exists())

    def test_shared_daily_limit_blocks_before_http_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.json"
            now = datetime(2026, 9, 15, 9, tzinfo=CN_TZ)
            first = ApiBudget(path=path, daily_limit=2, default_task="video",
                              now_fn=lambda: now)
            second = ApiBudget(path=path, daily_limit=2, default_task="comments",
                               now_fn=lambda: now)

            first.consume("/video")
            second.consume("/comments")
            with self.assertRaisesRegex(ApiBudgetExceeded, "2 次"):
                second.consume("/replies")

            stored = json.loads(path.read_text(encoding="utf-8"))
            day = stored["days"]["2026-09-15"]
            self.assertEqual(2, day["used"])
            self.assertEqual(1, day["blocked_attempts"])
            self.assertEqual({"video": 1, "comments": 1}, day["by_task"])

    def test_new_day_receives_fresh_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.json"
            clock = [datetime(2026, 9, 15, 23, tzinfo=CN_TZ)]
            budget = ApiBudget(path=path, daily_limit=1, now_fn=lambda: clock[0])
            budget.consume("/first", task="comments")
            clock[0] = datetime(2026, 9, 16, 0, 1, tzinfo=CN_TZ)
            budget.consume("/second", task="comments")

            snapshot = budget.snapshot()
            self.assertEqual("2026-09-16", snapshot["date"])
            self.assertEqual(1, snapshot["used"])
            self.assertEqual(2, len(snapshot["history"]))


if __name__ == "__main__":
    unittest.main()
