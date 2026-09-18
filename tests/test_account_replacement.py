"""Identity switching must be authorized, atomic and preserve comment evidence."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import fetch_data
from providers import history
from providers.base import Collection

KEY = 'douyin:测试'
ACCOUNT = {'platform': 'douyin', 'account_name': '测试', 'business_line': '芯片', 'platform_uid': 'new'}
SETTINGS = {'replaces_platform_uid': 'old', 'expected_uid': '123'}
PROFILE = {'verified_account_id': 'new', 'official_user_id': '123', 'followers': 8}


def video(cid):
    return {'video_id': str(cid), 'account_key': KEY, 'platform': 'douyin', 'stats': {'play': 10, 'like': 2, 'comment': 1}}


def content(cid, key=KEY):
    return {'content_id': str(cid), 'account_key': key, 'platform': 'douyin'}


class ReplacementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'accounts': [ACCOUNT]}))
        self.out = self.root / 'dashboard.json'
        self.old = {'accounts': [{**ACCOUNT, 'account_key': KEY, 'platform_uid': 'old', 'official_user_id': '456'}],
                    'videos': [video(i) for i in range(32)]}
        self.out.write_text(json.dumps(self.old))
        self.stack = [patch.object(history, 'DATA', self.root), patch.object(fetch_data, 'CONFIG_PATH', self.config),
                      patch.object(fetch_data, 'record_verification')]
        for p in self.stack:
            p.start()
            self.addCleanup(p.stop)

    def collect(self, complete=True, settings=None, profile=None, rows=None):
        result = Collection(profile or PROFILE, rows if rows is not None else [video(100)], complete=complete)
        provider = SimpleNamespace(collect=lambda **kw: result)
        registry = SimpleNamespace(config={'accounts': {KEY: SETTINGS if settings is None else settings}},
                                   call_count=0, get=lambda account: provider)
        return fetch_data.collect(SimpleNamespace(out=self.out, no_enrich_bili=True), registry)

    def assert_preserved(self, result):
        self.assertEqual('stale', result['accounts'][0]['status'])
        self.assertEqual('old', result['accounts'][0]['platform_uid'])
        self.assertEqual('456', result['accounts'][0]['official_user_id'])
        self.assertEqual(32, len(result['videos']))
        self.assertNotIn('account_replacement_id', result['accounts'][0])
        self.assertEqual(self.old, json.loads(self.out.read_text()))

    def test_authorized_complete_replacement_excludes_old_drop_and_metrics(self):
        result = self.collect(rows=[{**video(1), 'stats': {'play': None}}])
        self.assertEqual('ok', result['accounts'][0]['status'])
        self.assertEqual(1, len(result['videos']))
        self.assertIsNone(result['videos'][0]['stats']['play'])
        digest = result['accounts'][0]['account_replacement_id']
        archive = self.root / 'account_replacements' / (digest + '.json')
        self.assertEqual(0o600, archive.stat().st_mode & 0o777)
        self.assertEqual(32, len(json.loads(archive.read_text())['previous_records']))
        # collect itself never commits the snapshot or modifies comment state.
        self.assertEqual(self.old, json.loads(self.out.read_text()))

    def test_unapproved_partial_and_wrong_identity_preserve_old_snapshot(self):
        for kwargs in ({'settings': {}}, {'settings': {**SETTINGS, 'replaces_platform_uid': 'other'}},
                       {'complete': False}, {'profile': {**PROFILE, 'official_user_id': '999'}},
                       {'profile': {**PROFILE, 'verified_account_id': 'other'}}):
            with self.subTest(kwargs=kwargs):
                self.assert_preserved(self.collect(**kwargs))
        self.assertFalse((self.root / 'account_replacements').exists())

    def test_failure_after_archive_preserves_original_snapshot(self):
        with patch.object(fetch_data, 'retain_known', side_effect=RuntimeError('later failure')):
            self.assert_preserved(self.collect())
        self.assertEqual(1, len(list((self.root / 'account_replacements').glob('*.json'))))
        state = {'discovered_contents': [content(1)]}
        self.assertEqual([content(1)], history.retire_replaced_comment_contents([content(1)], state, {}, self.old))
        self.assertEqual([content(1)], state['discovered_contents'])

    def test_partial_channel_identity_conflict_preserves_old_catalog(self):
        key = 'wechat_channels:测试'
        account = {**ACCOUNT, 'platform': 'wechat_channels', 'platform_uid': 'sph_verified'}
        self.config.write_text(json.dumps({'accounts': [account]}))
        self.old['accounts'] = [{**account, 'account_key': key, 'official_user_id': 'v2_old@finder'}]
        self.old['videos'] = [{**r, 'platform': 'wechat_channels', 'account_key': key} for r in self.old['videos']]
        self.out.write_text(json.dumps(self.old))
        profile = {'verified_account_id': 'sph_verified', 'official_user_id': 'v2_new@finder'}
        rows = [{**video('export/new'), 'platform': 'wechat_channels', 'account_key': key}]
        partial = self.collect(complete=False, profile=profile, rows=rows)
        self.assertEqual('stale', partial['accounts'][0]['status'])
        self.assertEqual('v2_old@finder', partial['accounts'][0]['official_user_id'])
        self.assertEqual(32, len(partial['videos']))
        self.assertEqual(self.old, json.loads(self.out.read_text()))
        complete = self.collect(complete=True, profile=profile, rows=rows)
        self.assertEqual('ok', complete['accounts'][0]['status'])
        self.assertEqual(['export/new'], [r['video_id'] for r in complete['videos']])

    def test_marker_survives_subsequent_success_and_failure(self):
        first = self.collect()
        self.out.write_text(json.dumps(first))
        second = self.collect()
        self.assertEqual(first['accounts'][0]['account_replacement_id'], second['accounts'][0]['account_replacement_id'])
        with patch.object(fetch_data, 'retain_known', side_effect=RuntimeError('later failure')):
            stale = self.collect()
        self.assertEqual('stale', stale['accounts'][0]['status'])
        self.assertEqual(first['accounts'][0]['account_replacement_id'], stale['accounts'][0]['account_replacement_id'])

    def test_empty_replacement_retains_identity_and_retirement_on_later_failure(self):
        first = self.collect(rows=[])
        self.assertEqual('ok', first['accounts'][0]['status'])
        self.out.write_text(json.dumps(first))
        with patch.object(fetch_data, 'retain_known', side_effect=RuntimeError('later failure')):
            stale = self.collect(rows=[])
        self.assertEqual('stale', stale['accounts'][0]['status'])
        self.assertEqual([], stale['videos'])
        for field in ('platform_uid', 'official_user_id', 'verified_account_id', 'account_replacement_id'):
            self.assertEqual(first['accounts'][0][field], stale['accounts'][0][field])

    def test_comment_retirement_archives_first_preserves_overlap_and_is_idempotent(self):
        snapshot = self.collect(rows=[video(1), video(100)])
        state = {'seen_comments': {'douyin:0': ['c0'], 'douyin:1': ['c1'], 'douyin:100': ['c100']},
                 'content_counts': {'douyin:0': 1, 'douyin:1': 1},
                 'content_poll_at': {'douyin:0': 'now'},
                 'root_reply_counts': {'douyin:0:c0': 3, 'douyin:1:c1': 2, 'douyin:100:c100': 1},
                 'full_scan_baselines': ['douyin:0', 'douyin:1'],
                 'discovered_contents': [content(0), content(1), content(100)], 'baseline_done': True}
        timeline = {'events': {'c0': content(0), 'c1': content(1), 'other': content(0, 'douyin:另一账号')},
                    'timeline_started_at': 'original'}
        before_state, before_timeline = copy.deepcopy(state), copy.deepcopy(timeline)
        with patch.object(history, 'private_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                history.retire_replaced_comment_contents([content(0)], state, timeline, snapshot)
        self.assertEqual(before_state, state)
        self.assertEqual(before_timeline, timeline)
        kept = history.retire_replaced_comment_contents([content(0), content(1), content(100)], state, timeline, snapshot)
        self.assertEqual([content(1), content(100)], kept)
        self.assertNotIn('douyin:0', state['seen_comments'])
        self.assertIn('douyin:1', state['seen_comments'])
        self.assertNotIn('douyin:0:c0', state['root_reply_counts'])
        self.assertIn('douyin:100:c100', state['root_reply_counts'])
        self.assertEqual({'c1', 'other'}, set(timeline['events']))
        self.assertEqual('original', timeline['timeline_started_at'])
        archives = list((self.root / 'account_replacements' / 'comments').glob('*.json'))
        self.assertEqual(1, len(archives))
        self.assertEqual(0o600, archives[0].stat().st_mode & 0o777)
        self.assertEqual(['c0'], json.loads(archives[0].read_text())['state']['seen_comments']['douyin:0'])
        after = copy.deepcopy((state, timeline))
        self.assertEqual([], history.retire_replaced_comment_contents([content(0)], state, timeline, snapshot))
        self.assertEqual(after, (state, timeline))
        self.assertEqual(1, len(list(archives[0].parent.glob('*.json'))))

    def test_comment_current_content_and_other_accounts_protect_shared_state(self):
        snapshot = self.collect()
        snapshot['videos'] += [video(1), {**video(2), 'account_key': 'douyin:另一账号'}]
        state = {'seen_comments': {'douyin:1': ['c1'], 'douyin:2': ['c2']}}
        timeline = {'events': {'c1': content(1), 'c2': content(2)}}
        kept = history.retire_replaced_comment_contents([content(1), content(2)], state, timeline, snapshot)
        self.assertEqual([content(1)], kept)
        self.assertEqual({'douyin:1', 'douyin:2'}, set(state['seen_comments']))
        self.assertEqual({'c1'}, set(timeline['events']))

    def test_comment_mismatched_archive_fails_closed(self):
        snapshot = self.collect()
        snapshot['accounts'][0]['official_user_id'] = '999'
        with self.assertRaises(Exception):
            history.retire_replaced_comment_contents([], {}, {}, snapshot)


if __name__ == '__main__':
    unittest.main()
