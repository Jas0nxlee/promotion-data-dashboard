import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from api_budget import ApiBudget, ApiBudgetExceeded


CN_TZ = timezone(timedelta(hours=8))


class ApiBudgetTests(unittest.TestCase):
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
