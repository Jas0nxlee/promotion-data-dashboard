import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
from providers.base import Collection
from providers.history import retain_known
from providers.health import record_verification, read_verification


class MetricUnitTests(unittest.TestCase):
    def test_incompatible_counts_never_refill_current_users(self):
        old = [{'article_id': '1', 'stats': {'read': 120, 'like': 2, 'comment': 1}, 'data_source': 'old', 'fetched_at': 'then'}]
        incoming = [{'article_id': '1', 'stats': {'read': None, 'like': None, 'comment': None},
                     'extra_metrics': {'read_users': 90, 'like_users': 2},
                     'metric_provenance': {k: {'missing_reason': 'incompatible_unit'} for k in ('read', 'like')}}]
        row = retain_known(incoming, old, 'article_id')[0]
        self.assertIsNone(row['stats']['read'])
        self.assertIsNone(row['stats']['like'])
        self.assertEqual(90, row['extra_metrics']['read_users'])
        self.assertEqual(120, row['historical_metrics']['read']['value'])
        self.assertEqual(1, row['stats']['comment'])
        self.assertNotIn('stats.read', row.get('cached_fields', []))
        again = retain_known(incoming, [row], 'article_id')[0]
        self.assertEqual(row['historical_metrics'], again['historical_metrics'])

    def test_required_extra_metrics_are_checked_in_own_namespace(self):
        settings = {'provider': 'wechat_browser'}
        records = [{'stats': {'read': None, 'like': None, 'comment': 0},
                    'extra_metrics': {'read_users': 0, 'like_users': 0, 'share_users': 0}}]
        collection = Collection({'verified_account_id': 'gh_test'}, records, True)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            value = record_verification('wechat_service:test', settings, collection, directory=folder)
            self.assertEqual(1, value['metric_coverage']['extra_metrics.read_users'])
            self.assertNotIn('read', value['metric_coverage'])
            self.assertTrue(read_verification('wechat_service:test', settings, directory=folder)['ready'])
            records[0]['extra_metrics']['read_users'] = None
            record_verification('wechat_service:test', settings, collection, directory=folder)
            self.assertFalse(read_verification('wechat_service:test', settings, directory=folder)['ready'])
