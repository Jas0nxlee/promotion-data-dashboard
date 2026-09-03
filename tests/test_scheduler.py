import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import scheduler


CN_TZ = timezone(timedelta(hours=8))


class SchedulerTests(unittest.TestCase):
    def test_startup_runs_comment_baseline_even_when_not_top_of_hour(self):
        current = datetime(2026, 9, 2, 16, 23, tzinfo=CN_TZ)
        marks = []
        with mock.patch.object(scheduler, "load_state", return_value={}), \
                mock.patch.object(scheduler, "run_comment_cycle", return_value={"monitor": 0}), \
                mock.patch.object(scheduler, "run_data_collection") as data_run, \
                mock.patch.object(scheduler, "mark", side_effect=lambda *args: marks.append(args)):
            scheduler.run_due(current)
        data_run.assert_not_called()
        self.assertEqual("last_comment_slot", marks[0][1])
        self.assertEqual("2026-09-02T16", marks[0][2])

    def test_eight_oclock_runs_data_before_comment(self):
        current = datetime(2026, 9, 2, 8, 0, tzinfo=CN_TZ)
        events = []

        def fake_mark(_state, key, _slot, _results):
            events.append(key)

        with mock.patch.object(scheduler, "load_state", return_value={}), \
                mock.patch.object(scheduler, "run_data_collection", return_value={"video": 0}), \
                mock.patch.object(scheduler, "run_comment_cycle", return_value={"monitor": 0}), \
                mock.patch.object(scheduler, "mark", side_effect=fake_mark):
            scheduler.run_due(current)
        self.assertEqual(["last_data_slot", "last_comment_slot"], events)


if __name__ == "__main__":
    unittest.main()
