import sys
import unittest
import tempfile
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import scheduler


CN_TZ = timezone(timedelta(hours=8))


class SchedulerTests(unittest.TestCase):
    def test_session_alerts_run_every_five_minutes_while_collectors_are_busy(self):
        calls = []
        executor = mock.Mock()

        def submit(function):
            future = Future()
            calls.append((function.__name__, future))
            return future

        executor.submit.side_effect = submit
        engine = scheduler.DispatchScheduler(executor)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        current = datetime(2026, 10, 9, 10, 1, tzinfo=CN_TZ)
        with mock.patch.dict('os.environ', {'SESSION_ALERT_RECIPIENT': 'linwei.li@ucas.com.cn'}), \
                mock.patch.object(scheduler, 'load_state', return_value={}), \
                mock.patch.object(scheduler, 'save_state'), \
                mock.patch.object(scheduler, 'DATA', Path(temporary.name)):
            engine.tick(current)
            self.assertEqual(['run_comment_cycle', 'run_data_collection', 'run_session_alert_cycle'],
                             [name for name, _ in calls])
            calls[2][1].set_result({'alerts': 0})
            engine.tick(current.replace(minute=5))
            self.assertEqual('run_session_alert_cycle', calls[-1][0])
            self.assertEqual(4, len(calls))

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
