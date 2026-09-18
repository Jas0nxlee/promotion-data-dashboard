"""Candidate authorization isolation and rollback, using only temporary files and mocked browsers."""
from contextlib import contextmanager
from dataclasses import asdict
import json
import fcntl
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import authorization_worker
import login_manager
import runtime
from providers import browser as browser_module
from providers.authorization import AuthorizationStore
from providers.base import Collection, ProviderError
from providers.browser import session_key
from providers.credentials import private_json
from providers.health import read_verification, record_verification
from providers.settings import SettingsStore

FIRST = 'baijiahao:fixture-first'
SECOND = 'xiaohongshu:fixture-second'
PUBLIC = 'csdn:fixture-public'


class LoginManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='promotion-login-manager-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace, self.sessions, self.data = self.root / 'operations', self.root / 'sessions', self.root / 'data'
        self.sessions.mkdir()
        self.store = SettingsStore(self.root / 'providers.json')
        self.old_settings = {'provider': 'baijiahao_creator', 'channel': 'chrome',
                             'cdp_url': 'http://127.0.0.1:61000', 'session_mode': 'interactive'}
        self.other_settings = {'provider': 'xiaohongshu_creator', 'channel': 'chrome'}
        self.store.update(FIRST, self.old_settings)
        self.store.update(SECOND, self.other_settings)
        self.target = self.sessions / (session_key(FIRST) + '.storage.json')
        self.other_target = self.sessions / (session_key(SECOND) + '.storage.json')
        self.old_session = {'cookies': [{'name': 'fixture', 'value': 'old-test', 'domain': '.baidu.com'}], 'origins': []}
        private_json(self.target, self.old_session)
        private_json(self.other_target, {'cookies': [], 'origins': []})
        self.catalog = {
            key: {'platform': key.split(':')[0], 'account_name': key.split(':')[1], 'platform_uid': 'canonical'}
            for key in (FIRST, SECOND, PUBLIC)}
        self.entries = {'baijiahao': 'https://baijiahao.baidu.com/', 'xiaohongshu': 'https://creator.xiaohongshu.com/'}
        self.auth = AuthorizationStore(self.sessions / 'authorization')
        self.process = Mock(pid=12345)
        self.process.poll.return_value = None
        self.process.wait.return_value = 0
        self.popen = Mock(return_value=self.process)
        self.executor = Mock()
        patches = [patch.object(browser_module, 'SESSIONS', self.sessions),
                   patch.object(login_manager, 'ThreadPoolExecutor', return_value=self.executor),
                   patch.object(login_manager.LoginManager, '_stop'),
                   patch('playwright.sync_api.sync_playwright')]
        mocks = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        self.stop = mocks[2]
        mocks[3].return_value.__enter__.return_value.chromium.executable_path = '/fixture/chromium'
        self.manager = self.new_manager()
        self.addCleanup(self.manager.close)

    def new_manager(self):
        return login_manager.LoginManager(self.store, self.catalog, self.entries,
                                         lambda account, settings: dict(settings), workspace=self.workspace,
                                         sessions=self.sessions, data=self.data, authorization=self.auth,
                                         popen=self.popen, start_reaper=False)

    def start(self):
        return self.manager.start(FIRST)

    def result(self):
        return {'success': True, 'settings': {**self.old_settings, 'headed': True},
                'collection': asdict(Collection({'verified_account_id': 'canonical', 'total': 1},
                                               [{'article_id': '1', 'stats': {'read': 0, 'like': 0, 'comment': 0}}],
                                               source='baijiahao_creator')),
                'comments': None, 'comment_stats': None}

    def candidate(self):
        path = self.manager.active['folder'] / 'sessions' / self.target.name
        state = {'cookies': [{'name': 'fixture', 'value': 'new-test', 'domain': '.baidu.com'}], 'origins': []}
        private_json(path, state)
        return path, state

    def baseline(self):
        return self.store.path.read_bytes(), self.target.read_bytes(), self.other_target.read_bytes()

    def test_single_desktop_reuses_same_account_and_rejects_other_account(self):
        first = self.start()
        self.assertEqual(first['operation_id'], self.start()['operation_id'])
        with self.assertRaisesRegex(ProviderError, 'desktop_busy'):
            self.manager.start(SECOND)
        self.assertEqual(1, self.popen.call_count)
        self.assertTrue(first['desktop_url'].startswith('/desktop/'))
        self.assertEqual('awaiting_login', first['state'])
        self.manager.cancel(FIRST, first['operation_id'])
        with self.assertRaisesRegex(ProviderError, 'unsupported'):
            self.manager.start(PUBLIC)

    def test_authorization_timeout_is_bounded_and_persisted(self):
        from datetime import datetime
        for value in ['0','299','3601']:
            with patch.dict(login_manager.os.environ,{'PROMOTION_AUTHORIZATION_TTL_SECONDS':value}), \
                 self.assertRaisesRegex(ProviderError,'invalid_authorization_ttl'):
                self.new_manager()
        self.manager.close()
        with patch.dict(login_manager.os.environ,{'PROMOTION_AUTHORIZATION_TTL_SECONDS':'3600'}):
            self.manager=self.new_manager()
        self.addCleanup(self.manager.close)
        self.start()
        state=self.auth.read(FIRST)
        self.assertAlmostEqual(3600,state['expires_at_epoch']-datetime.fromisoformat(state['started_at']).timestamp(),places=4)

    def test_xiaohongshu_opens_creator_and_public_login_pages(self):
        self.manager.start(SECOND)
        command = self.popen.call_args.args[0]
        self.assertIn('https://creator.xiaohongshu.com/', command)
        self.assertIn('https://www.xiaohongshu.com/', command)

    def test_operation_and_account_mismatch_cannot_complete_or_cancel(self):
        state = self.start()
        before = self.baseline()
        for action in (self.manager.complete, self.manager.cancel):
            for account, operation in ((SECOND, state['operation_id']), (FIRST, 'wrong-operation')):
                with self.subTest(action=action.__name__, account=account), self.assertRaisesRegex(ProviderError, 'authorization_conflict'):
                    action(account, operation)
        self.executor.submit.assert_not_called()
        self.assertEqual(before, self.baseline())
        self.assertEqual(state['operation_id'], self.manager.status()['operation_id'])

    def test_verifying_cannot_repeat_complete_or_cancel(self):
        state = self.start()
        result = self.manager.complete(FIRST, state['operation_id'])
        self.assertEqual('verifying', result['status'])
        for action in (self.manager.complete, self.manager.cancel):
            with self.assertRaisesRegex(ProviderError, 'busy'):
                action(FIRST, state['operation_id'])
        self.executor.submit.assert_called_once_with(self.manager._verify, FIRST, state['operation_id'])

    def test_expired_authorization_releases_desktop_preserving_original_session(self):
        state = self.start()
        before = self.baseline()
        stored = self.auth.read(FIRST)
        stored['expires_at_epoch'] = 1
        private_json(self.auth._path(FIRST), stored)
        self.assertIsNone(self.manager.status())
        self.assertEqual('reauth_required', self.auth.read(FIRST)['status'])
        self.assertEqual('authorization_timeout', self.auth.read(FIRST)['reason'])
        self.assertFalse((self.workspace / state['operation_id']).exists())
        self.stop.assert_any_call(self.process)
        self.assertEqual(before, self.baseline())

    def test_browser_close_releases_desktop_and_marks_reauthorization_required(self):
        self.start()
        before = self.baseline()
        self.process.poll.return_value = 0
        self.assertIsNone(self.manager.status())
        self.assertEqual('browser_closed', self.auth.read(FIRST)['reason'])
        self.assertEqual(before, self.baseline())

    def test_failed_worker_or_wrong_identity_does_not_replace_existing_authorization(self):
        for reason, code, payload in (
            ('identity_mismatch', 2, {'success': False, 'reason': 'identity_mismatch', 'message': 'wrong account'}),
            ('verification_failed', 1, {'success': True}),
            ('verification_failed', 2, None),
        ):
            with self.subTest(reason=reason, code=code):
                state = self.start()
                self.candidate()
                before = self.baseline()
                worker = Mock(pid=23456)
                worker.wait.return_value = code
                worker.poll.return_value = code
                def launch(command, **kwargs):
                    if payload is not None:
                        private_json(Path(command[command.index('--output') + 1]), payload)
                    return worker
                self.popen.side_effect = launch
                self.manager.complete(FIRST, state['operation_id'])
                self.manager._verify(FIRST, state['operation_id'])
                self.assertEqual('error', self.manager.status()['state'])
                self.assertIn(reason, self.manager.status()['message'])
                self.assertEqual(before, self.baseline())
                self.assertEqual('authorizing', self.auth.read(FIRST)['status'])
                self.manager.cancel(FIRST, state['operation_id'])
                self.popen.side_effect = None

    def test_promote_replaces_only_target_and_removes_interactive_metadata(self):
        self.start()
        _, candidate = self.candidate()
        other_before = self.other_target.read_bytes()
        self.manager._promote(self.manager.active, self.result())
        final = self.store.read()['accounts'][FIRST]
        self.assertEqual('portable', final['session_mode'])
        self.assertEqual('chromium', final['channel'])
        self.assertNotIn('cdp_url', final)
        self.assertNotIn('headed', final)
        self.assertEqual(self.other_settings, self.store.read()['accounts'][SECOND])
        self.assertEqual(other_before, self.other_target.read_bytes())
        self.assertEqual(candidate, json.loads(self.target.read_text()))
        self.assertEqual(0o600, self.target.stat().st_mode & 0o777)
        self.assertEqual('authorized', self.auth.read(FIRST)['status'])
        self.assertTrue(read_verification(FIRST, final, self.data / 'verification')['ready'])
        self.assertIsNone(self.manager.status())

    def test_concurrent_configuration_change_rejects_promotion_without_session_change(self):
        self.start()
        self.candidate()
        changed = {**self.old_settings, 'expected_uid': 'operator-edited'}
        self.store.update(FIRST, changed)
        before = self.baseline()
        with self.assertRaisesRegex(ProviderError, 'config_changed'):
            self.manager._promote(self.manager.active, self.result())
        self.assertEqual(before, self.baseline())
        self.assertEqual(changed, self.store.read()['accounts'][FIRST])

    def test_session_write_failure_rolls_back_configuration_and_previous_session(self):
        self.start()
        self.candidate()
        before = self.baseline()
        failed = False
        def write(path, payload):
            nonlocal failed
            if path == self.target and not failed:
                failed = True
                raise OSError('fixture disk error')
            return private_json(path, payload)
        with patch.object(login_manager, 'private_json', side_effect=write), self.assertRaisesRegex(OSError, 'disk error'):
            self.manager._promote(self.manager.active, self.result())
        self.assertEqual(before, self.baseline())
        self.assertEqual('authorizing', self.auth.read(FIRST)['status'])

    def test_late_authorization_failure_rolls_back_health_and_existing_files(self):
        self.start()
        self.candidate()
        original_health = record_verification(FIRST, self.old_settings, error=ProviderError('fixture', 'old evidence'),
                                             directory=self.data / 'verification')
        before = self.baseline()
        with patch.object(self.auth, 'complete', side_effect=ProviderError('authorization_conflict', 'expired during commit')):
            with self.assertRaisesRegex(ProviderError, 'authorization_conflict'):
                self.manager._promote(self.manager.active, self.result())
        self.assertEqual(before, self.baseline())
        path = self.data / 'verification' / (session_key(FIRST) + '.json')
        self.assertEqual(original_health, json.loads(path.read_text()))

    def test_recovery_never_kills_pid_without_exact_profile_and_process_group(self):
        self.manager.close()
        read_bytes = Path.read_bytes
        for own_profile, own_group in ((False, True), (True, False), (True, True)):
            with self.subTest(own_profile=own_profile, own_group=own_group):
                state = self.auth.begin(FIRST)
                operation = state['operation_id']
                pid = 34567
                private_json(self.workspace / 'active.json', {'account': FIRST, 'operation_id': operation, 'pid': pid})
                profile = self.workspace / operation / 'profile' if own_profile else self.root / 'unrelated-profile'
                command = f'/fixture/chrome\0--user-data-dir={profile}\0'.encode()
                def read(path):
                    return command if str(path) == f'/proc/{pid}/cmdline' else read_bytes(path)
                with patch.object(Path, 'read_bytes', read), \
                     patch.object(login_manager.os, 'getpgid', return_value=pid if own_group else pid - 1), \
                     patch.object(login_manager.os, 'killpg') as kill:
                    recovered = self.new_manager()
                    try:
                        self.assertIsNone(recovered.status())
                        self.assertEqual('reauth_required', self.auth.read(FIRST)['status'])
                        if own_profile and own_group:
                            kill.assert_called_once_with(pid, signal.SIGTERM)
                        else:
                            kill.assert_not_called()
                    finally:
                        recovered.close()
                self.assertFalse((self.workspace / 'active.json').exists())

    def test_second_manager_cannot_recover_live_instance_or_kill_its_desktop(self):
        state = self.start()
        with patch.object(login_manager.os, 'killpg') as kill:
            with self.assertRaisesRegex(ProviderError, 'authorization_service_busy'):
                self.new_manager()
        kill.assert_not_called()
        self.assertEqual(state['operation_id'], self.manager.status()['operation_id'])
        self.assertEqual('authorizing', self.auth.read(FIRST)['status'])

    def test_new_session_cannot_inherit_previous_comment_verification(self):
        from providers.health import record_comment_verification
        final = {'provider': 'baijiahao_creator', 'channel': 'chromium', 'session_mode': 'portable'}
        self.store.update(FIRST, final)
        self.start()
        self.candidate()
        collection = Collection(**self.result()['collection'])
        directory = self.data / 'verification'
        record_verification(FIRST, final, collection, directory=directory)
        record_comment_verification(FIRST, final, [{'parent_comment_id': 'old-root'}], directory=directory,
                                    stats={'comments_complete': True, 'replies_complete': True, 'expected_replies': 1})
        self.assertTrue(read_verification(FIRST, final, directory)['replies_verified'])
        self.manager._promote(self.manager.active, {**self.result(), 'settings': final})
        evidence = read_verification(FIRST, final, directory)
        self.assertFalse(evidence['comments_verified'])
        self.assertFalse(evidence['replies_verified'])
        self.assertNotIn('sample_comment_count', evidence)

    def test_initial_authorization_failure_removes_new_configuration_session_and_health(self):
        self.store.compare_update(FIRST, self.old_settings, None)
        self.target.unlink()
        state = self.start()
        self.candidate()
        with patch.object(self.auth, 'complete', side_effect=ProviderError('authorization_conflict', 'expired')):
            with self.assertRaises(ProviderError):
                self.manager._promote(self.manager.active, self.result())
        self.assertNotIn(FIRST, self.store.read()['accounts'])
        self.assertFalse(self.target.exists())
        self.assertFalse((self.data / 'verification' / (session_key(FIRST) + '.json')).exists())
        self.assertEqual(state['operation_id'], self.manager.status()['operation_id'])

    def test_successful_worker_promotes_and_closes_only_operation_processes(self):
        state = self.start()
        self.candidate()
        worker = Mock(pid=23456)
        worker.wait.return_value = 0
        worker.poll.return_value = 0
        def launch(command, **kwargs):
            private_json(Path(command[command.index('--output') + 1]), self.result())
            env = kwargs['env']
            folder = self.manager.active['folder']
            self.assertEqual(str(folder / 'sessions'), env['PROMOTION_SESSION_DIR'])
            self.assertEqual(str(folder / 'providers.json'), env['PROMOTION_PROVIDER_CONFIG'])
            self.assertEqual('1', env['PROMOTION_AUTHORIZATION_OPERATION'])
            return worker
        self.popen.side_effect = launch
        self.manager.complete(FIRST, state['operation_id'])
        self.manager._verify(FIRST, state['operation_id'])
        self.assertIsNone(self.manager.status())
        self.assertEqual('authorized', self.auth.read(FIRST)['status'])
        self.stop.assert_any_call(worker)
        self.stop.assert_any_call(self.process)
        self.assertFalse((self.workspace / state['operation_id']).exists())

    def test_worker_timeout_stops_candidate_but_preserves_existing_settings_and_session(self):
        import subprocess
        state = self.start()
        before = self.baseline()
        worker = Mock(pid=23456)
        worker.wait.side_effect = subprocess.TimeoutExpired('fixture-worker', 1)
        self.popen.return_value = worker
        self.manager.complete(FIRST, state['operation_id'])
        self.manager._verify(FIRST, state['operation_id'])
        self.assertEqual('error', self.manager.status()['state'])
        self.assertIn('verification_timeout', self.manager.status()['message'])
        self.stop.assert_any_call(worker)
        self.assertEqual(before, self.baseline())

    def test_browser_launch_failure_leaves_no_active_operation_or_candidate_profile(self):
        before = self.baseline()
        self.popen.side_effect = OSError('fixture missing browser')
        with self.assertRaisesRegex(ProviderError, 'browser_missing'):
            self.start()
        self.assertIsNone(self.manager.status())
        self.assertEqual('reauth_required', self.auth.read(FIRST)['status'])
        self.assertEqual([], [entry for entry in self.workspace.iterdir() if entry.is_dir()])
        self.assertEqual(before, self.baseline())

    def release_after_hard_crash(self):
        """Model OS lock release, deliberately skipping normal cancel/cleanup."""
        manager = self.manager
        manager.stopping.set()
        fcntl.flock(manager.instance_lock, fcntl.LOCK_UN)
        manager.instance_lock.close()
        manager.active = None

    def restart(self):
        self.release_after_hard_crash()
        self.manager = self.new_manager()
        self.addCleanup(self.manager.close)
        return self.manager

    def test_hard_crash_before_commit_restores_exact_config_session_and_health(self):
        class Crash(BaseException):
            pass
        health_path = self.data / 'verification' / (session_key(FIRST) + '.json')
        for window in ('journal', 'configuration', 'session', 'health'):
            with self.subTest(window=window):
                state = self.start()
                self.candidate()
                record_verification(FIRST, self.old_settings, error=ProviderError('old', 'previous evidence'),
                                    directory=self.data / 'verification')
                before = (*self.baseline(), health_path.read_bytes())
                real_update, real_health = self.store.compare_update, login_manager.record_verification
                def update(*args, **kwargs):
                    value = real_update(*args, **kwargs)
                    if window == 'configuration':
                        raise Crash()
                    return value
                def write(path, payload):
                    private_json(path, payload)
                    if (window == 'journal' and path.name == 'promotion-journal.json') or (window == 'session' and path == self.target):
                        raise Crash()
                def health(*args, **kwargs):
                    value = real_health(*args, **kwargs)
                    if window == 'health':
                        raise Crash()
                    return value
                with patch.object(self.store, 'compare_update', side_effect=update), \
                     patch.object(login_manager, 'private_json', side_effect=write), \
                     patch.object(login_manager, 'record_verification', side_effect=health):
                    with self.assertRaises(Crash):
                        self.manager._promote(self.manager.active, self.result())
                journal = self.workspace / state['operation_id'] / 'promotion-journal.json'
                self.assertTrue(journal.exists())
                self.assertEqual(0o600, journal.stat().st_mode & 0o777)
                self.restart()
                self.assertEqual(before, (*self.baseline(), health_path.read_bytes()))
                self.assertEqual('reauth_required', self.auth.read(FIRST)['status'])
                self.assertFalse((self.workspace / state['operation_id']).exists())
                self.assertFalse(self.manager.state_path.exists())

    def test_hard_crash_after_authorized_commit_keeps_new_files_and_evidence(self):
        class Crash(BaseException):
            pass
        state = self.start()
        _, candidate = self.candidate()
        complete = self.auth.complete
        def crash_after_commit(*args):
            complete(*args)
            raise Crash()
        with patch.object(self.auth, 'complete', side_effect=crash_after_commit), self.assertRaises(Crash):
            self.manager._promote(self.manager.active, self.result())
        new_files = self.baseline()
        health_path = self.data / 'verification' / (session_key(FIRST) + '.json')
        new_health = health_path.read_bytes()
        self.assertEqual('authorized', self.auth.read(FIRST)['status'])
        self.assertTrue((self.workspace / state['operation_id'] / 'promotion-journal.json').exists())
        self.restart()
        self.assertEqual(new_files, self.baseline())
        self.assertEqual(new_health, health_path.read_bytes())
        self.assertEqual(candidate, json.loads(self.target.read_text()))
        self.assertEqual('authorized', self.auth.read(FIRST)['status'])
        self.assertTrue(read_verification(FIRST, self.store.read()['accounts'][FIRST], health_path.parent)['ready'])
        self.assertFalse((self.workspace / state['operation_id']).exists())

    def test_old_crash_journal_cannot_overwrite_superseding_operation(self):
        class Crash(BaseException):
            pass
        old = self.start()
        self.candidate()
        with patch.object(self.auth, 'complete', side_effect=Crash()), self.assertRaises(Crash):
            self.manager._promote(self.manager.active, self.result())
        self.auth.cancel(FIRST, old['operation_id'])
        new = self.auth.begin(FIRST)
        self.assertNotEqual(old['operation_id'], new['operation_id'])
        newer_settings = {'provider': 'baijiahao_creator', 'session_mode': 'portable', 'expected_uid': 'new-operation'}
        self.store.update(FIRST, newer_settings)
        private_json(self.target, {'cookies': [], 'origins': [], 'fixture': 'new-operation'})
        health_path = self.data / 'verification' / (session_key(FIRST) + '.json')
        private_json(health_path, {'fixture': 'new-operation-evidence'})
        new_files, new_health = self.baseline(), health_path.read_bytes()
        self.restart()
        self.assertEqual(new_files, self.baseline())
        self.assertEqual(new_health, health_path.read_bytes())
        self.assertEqual(new['operation_id'], self.auth.read(FIRST)['operation_id'])
        self.assertEqual('authorizing', self.auth.read(FIRST)['status'])
        self.assertFalse((self.workspace / old['operation_id']).exists())

    def test_worker_pid_is_persisted_before_wait_and_recovered_only_for_matching_operation(self):
        class Crash(BaseException):
            pass
        read_bytes = Path.read_bytes
        for mismatch in ('account', 'output', 'script', 'group', None):
            with self.subTest(mismatch=mismatch):
                state = self.start()
                folder = self.manager.active['folder']
                worker = Mock(pid=45678)
                worker.wait.side_effect = Crash()
                self.popen.return_value = worker
                self.manager.complete(FIRST, state['operation_id'])
                with self.assertRaises(Crash):
                    self.manager._verify(FIRST, state['operation_id'])
                persisted = json.loads(self.manager.state_path.read_text())
                self.assertEqual(worker.pid, persisted['worker_pid'])
                args = [sys.executable, str(login_manager.ROOT / 'pipeline/authorization_worker.py'),
                        '--account', FIRST, '--output', str(folder / 'result.json')]
                if mismatch == 'account':
                    args[3] = SECOND
                elif mismatch == 'output':
                    args[5] = str(self.root / 'unrelated' / 'result.json')
                elif mismatch == 'script':
                    args[1] = '/unrelated/worker.py'
                def read(path):
                    if str(path) == f'/proc/{worker.pid}/cmdline':
                        return ('\0'.join(args) + '\0').encode()
                    if str(path) == f'/proc/{self.process.pid}/cmdline':
                        return b'/unrelated/browser\0'
                    return read_bytes(path)
                with patch.object(Path, 'read_bytes', read), \
                     patch.object(login_manager.os, 'getpgid', return_value=worker.pid if mismatch != 'group' else worker.pid - 1), \
                     patch.object(login_manager.os, 'killpg') as kill:
                    self.restart()
                    if mismatch is None:
                        kill.assert_called_once_with(worker.pid, signal.SIGTERM)
                    else:
                        kill.assert_not_called()
                self.popen.return_value = self.process

    def test_permanent_rollback_failure_preserves_journal_and_blocks_new_authorization(self):
        state = self.start()
        self.candidate()
        with patch.object(self.auth, 'complete', side_effect=OSError('fixture commit failure')), \
             patch.object(self.manager, '_restore', side_effect=OSError('fixture persistent storage failure')):
            with self.assertRaises(OSError):
                self.manager._promote(self.manager.active, self.result())
            journal = self.manager.active['folder'] / 'promotion-journal.json'
            self.assertTrue(journal.exists())
            before = journal.read_bytes()
            self.manager.cancel(FIRST, state['operation_id'])
            self.assertTrue(self.manager.recovery_required)
            self.assertEqual('error', self.manager.status()['state'])
            self.assertIsNone(self.manager.status()['desktop_url'])
            self.assertTrue(self.manager.state_path.exists())
            self.assertEqual(before, journal.read_bytes())
            with self.assertRaises(ProviderError):
                self.manager.start(SECOND)
            self.assertEqual(state['operation_id'], self.manager.start(FIRST)['operation_id'])
            self.assertEqual(1, self.popen.call_count)
            for action in (self.manager.complete, self.manager.cancel):
                with self.assertRaisesRegex(ProviderError, 'recovery_required'):
                    action(FIRST, state['operation_id'])
            self.assertEqual(before, journal.read_bytes())
        self.restart()
        self.assertEqual(self.old_settings, self.store.read()['accounts'][FIRST])
        self.assertEqual(self.old_session, json.loads(self.target.read_text()))
        self.assertFalse(journal.exists())

    def test_own_deadline_releases_desktop_even_if_store_deadline_was_extended(self):
        state = self.start()
        self.manager.active['expires_at_epoch'] = 1
        self.assertGreater(self.auth.read(FIRST)['expires_at_epoch'], 1)
        self.assertIsNone(self.manager.status())
        self.assertEqual('reauth_required', self.auth.read(FIRST)['status'])
        self.assertFalse((self.workspace / state['operation_id']).exists())
        self.stop.assert_any_call(self.process)

    def test_changed_operation_closes_old_desktop_without_cancelling_new_operation(self):
        state = self.start()
        self.auth.cancel(FIRST, state['operation_id'])
        newer = self.auth.begin(FIRST)
        self.assertIsNone(self.manager.status())
        self.assertEqual(newer['operation_id'], self.auth.read(FIRST)['operation_id'])
        self.assertEqual('authorizing', self.auth.read(FIRST)['status'])
        self.assertFalse((self.workspace / state['operation_id']).exists())
        self.stop.assert_any_call(self.process)

    def test_forged_nonempty_identity_cannot_promote_or_write_recovery_journal(self):
        self.start()
        self.candidate()
        before = self.baseline()
        forged = self.result()
        forged['collection']['profile']['verified_account_id'] = 'different-nonempty-account'
        with self.assertRaisesRegex(ProviderError, 'identity_mismatch'):
            self.manager._promote(self.manager.active, forged)
        self.assertEqual(before, self.baseline())
        self.assertFalse((self.manager.active['folder'] / 'promotion-journal.json').exists())
        self.assertEqual('authorizing', self.auth.read(FIRST)['status'])


class AuthorizationWorkerTests(unittest.TestCase):
    def test_cli_allows_large_history_but_preserves_explicit_page_cap(self):
        for extra, expected in [([],500),(['--max-pages','3'],3)]:
            with tempfile.TemporaryDirectory() as tmp:
                output=Path(tmp)/'result.json'
                with patch.object(sys,'argv',['authorization_worker.py','--account',FIRST,'--output',str(output),*extra]), \
                     patch.object(authorization_worker,'validate_candidate',return_value={'success':True}) as validate, \
                     self.assertRaises(SystemExit) as raised:
                    authorization_worker.main()
                self.assertEqual(0,raised.exception.code)
                validate.assert_called_once_with(FIRST,expected)

    def test_error_message_does_not_duplicate_reason_at_manager_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'result.json'
            with patch.object(sys, 'argv', ['authorization_worker.py', '--account', FIRST, '--output', str(output)]), \
                 patch.object(authorization_worker, 'validate_candidate',
                              side_effect=ProviderError('verification_incomplete', '评论尚未完整')), \
                 self.assertRaises(SystemExit) as raised:
                authorization_worker.main()
            self.assertEqual(2, raised.exception.code)
            result = json.loads(output.read_text())
            self.assertEqual('verification_incomplete', result['reason'])
            self.assertEqual('评论尚未完整', result['message'])

    def test_request_budget_failure_does_not_ask_for_another_login(self):
        from api_budget import ApiBudgetExceeded
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'result.json'
            with patch.object(sys, 'argv', ['authorization_worker.py', '--account', FIRST, '--output', str(output)]), \
                 patch.object(authorization_worker, 'validate_candidate', side_effect=ApiBudgetExceeded('fixture quota')), \
                 self.assertRaises(SystemExit) as raised:
                authorization_worker.main()
            self.assertEqual(2, raised.exception.code)
            result = json.loads(output.read_text())
            self.assertFalse(result['success'])
            self.assertEqual('budget_exceeded', result['reason'])
            self.assertIn('无需因此重新扫码', result['message'])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='promotion-authorization-worker-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SettingsStore(self.root / 'candidate' / 'providers.json')
        self.original = self.root / 'original.storage.json'
        private_json(self.original, {'cookies': [], 'origins': [], 'fixture': 'untouched'})
        self.original_bytes = self.original.read_bytes()
        patches = [patch.object(authorization_worker, 'SettingsStore', return_value=self.store),
                   patch.object(runtime, 'PROVIDER_CONFIG', self.store.path),
                   patch.object(browser_module, 'SESSIONS', self.root / 'sessions')]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self):
        self.assertEqual(self.original_bytes, self.original.read_bytes())

    def setup_candidate(self, platform='baijiahao', *, comments=None, stats=None, complete=True, identity='canonical', metrics=None):
        key = platform + ':fixture'
        account = {'platform': platform, 'account_name': 'fixture', 'platform_uid': 'canonical'}
        kinds = {'baijiahao': 'baijiahao_creator', 'bilibili': 'bilibili_creator', 'douyin': 'douyin_creator',
                 'wechat_channels': 'wechat_channels_creator', 'xiaohongshu': 'xiaohongshu_creator'}
        settings = {'provider': kinds[platform], 'cdp_url': 'http://127.0.0.1:61001', 'headed': True,
                    'session_mode': 'interactive', 'expected_finder_id': 'fixture-finder'}
        self.store.update(key, settings)
        self.events = []
        self.export_path = self.root / 'sessions' / (session_key(key) + '.storage.json')
        interactive, portable = Mock(), Mock()
        interactive.browser.settings = dict(settings)
        @contextmanager
        def session():
            self.events.append('session-enter')
            yield
            self.events.append('session-exit')
        def export():
            self.events.append('export')
            private_json(self.export_path, {'cookies': [], 'origins': []})
            return self.export_path
        interactive.browser.session.side_effect = session
        interactive._profile.side_effect = lambda: (self.events.append('identity') or {'verified_account_id': 'canonical'})
        interactive.browser.export_session.side_effect = export
        metric_values = {'read': 2, 'play': 2, 'like': 0, 'comment': 1, 'share': 0} if metrics is None else metrics
        id_key = 'article_id' if platform in {'baijiahao', 'xiaohongshu'} else 'video_id'
        records = [{id_key: 'no-comments', 'stats': {**metric_values, 'comment': 0}},
                   {id_key: 'with-comments', 'stats': metric_values}]
        collection = Collection({'verified_account_id': identity, 'total': 2}, records, complete=complete, source=kinds[platform])
        def collect(**kwargs):
            self.assertTrue(self.export_path.exists())
            current = self.store.read()['accounts'][key]
            self.assertEqual('portable', current['session_mode'])
            self.assertNotIn('cdp_url', current)
            self.assertNotIn('headed', current)
            self.events.append('portable-collect')
            return collection
        portable.collect.side_effect = collect
        portable.comments.return_value = (
            [{'parent_comment_id': '', 'reply_count': 0, 'text': 'private fixture text', 'author': {'name': 'private'}}]
            if comments is None else comments,
            {'comments_complete': True, 'replies_complete': True, 'expected_replies': 0} if stats is None else stats)
        registry = Mock(side_effect=[SimpleNamespace(get=Mock(return_value=interactive)),
                                    SimpleNamespace(get=Mock(return_value=portable))])
        return key, account, interactive, portable, registry

    @contextmanager
    def worker(self, setup):
        key, account, interactive, portable, registry = setup
        with patch.object(authorization_worker, 'accounts', return_value={key: account}), \
             patch.object(authorization_worker, 'ProviderRegistry', registry):
            yield key, interactive, portable

    def test_identity_then_export_then_fresh_portable_collection_is_required(self):
        setup = self.setup_candidate()
        with self.worker(setup) as (key, interactive, portable):
            result = authorization_worker.validate_candidate(key, 200)
        self.assertEqual(['session-enter', 'identity', 'export', 'session-exit', 'portable-collect'], self.events)
        self.assertTrue(result['success'])
        self.assertEqual('portable', result['settings']['session_mode'])
        self.assertNotIn('cdp_url', result['settings'])
        portable.collect.assert_called_once_with(max_pages=200)
        portable.comments.assert_not_called()

    def test_new_channels_binding_requires_metrics_without_manual_metric_config(self):
        setup = self.setup_candidate('wechat_channels', metrics={'like': 0, 'comment': 1})
        with self.worker(setup) as (key, _, portable), self.assertRaisesRegex(ProviderError, '必要指标'):
            authorization_worker.validate_candidate(key)
        portable.comments.assert_not_called()

    def test_all_four_detail_platforms_require_comments_from_restored_session_and_strip_text(self):
        for platform in ('bilibili', 'douyin', 'wechat_channels', 'xiaohongshu'):
            with self.subTest(platform=platform):
                setup = self.setup_candidate(platform)
                with self.worker(setup) as (key, interactive, portable):
                    result = authorization_worker.validate_candidate(key, 200)
                portable.comments.assert_called_once_with({**setup[1], 'content_id': 'with-comments'},
                                                         max_pages=200, include_replies=True)
                interactive.comments.assert_not_called()
                self.assertEqual([{'parent_comment_id': '', 'reply_count': 0}], result['comments'])
                self.assertEqual({'comments_complete': True, 'replies_complete': True, 'expected_replies': 0}, result['comment_stats'])

    def test_wrong_identity_stops_before_export_and_portable_restore(self):
        setup = self.setup_candidate()
        setup[2]._profile.side_effect = ProviderError('identity_mismatch', 'fixture wrong account')
        with self.worker(setup) as (key, interactive, portable), self.assertRaisesRegex(ProviderError, 'identity_mismatch'):
            authorization_worker.validate_candidate(key)
        interactive.browser.export_session.assert_not_called()
        portable.collect.assert_not_called()
        self.assertFalse(self.export_path.exists())

    def test_missing_identity_partial_collection_and_unknown_required_metrics_reject_candidate(self):
        cases = ({'identity': ''}, {'complete': False}, {'metrics': {'read': None, 'like': 0, 'comment': 0}})
        for change in cases:
            with self.subTest(change=change):
                setup = self.setup_candidate(**change)
                with self.worker(setup) as (key, interactive, portable), self.assertRaisesRegex(ProviderError, 'verification_incomplete'):
                    authorization_worker.validate_candidate(key)
                portable.comments.assert_not_called()

    def test_xiaohongshu_missing_public_login_rejects_candidate(self):
        setup = self.setup_candidate('xiaohongshu')
        setup[3].comments.side_effect = ProviderError('session_expired', 'fixture public login unavailable')
        with self.worker(setup) as (key, _, __), self.assertRaisesRegex(ProviderError, 'session_expired'):
            authorization_worker.validate_candidate(key)

    def test_partial_comment_or_reply_page_rejects_candidate(self):
        for missing in ('comments_complete', 'replies_complete'):
            stats = {'comments_complete': True, 'replies_complete': True, 'expected_replies': 0}
            stats[missing] = False
            setup = self.setup_candidate('xiaohongshu', stats=stats)
            with self.subTest(field=missing), self.worker(setup) as (key, _, __), self.assertRaises(ProviderError):
                authorization_worker.validate_candidate(key)

    def test_complete_empty_comments_allow_authorization_but_do_not_prove_reply_readiness(self):
        setup = self.setup_candidate('xiaohongshu', comments=[])
        with self.worker(setup) as (key, _, __):
            result = authorization_worker.validate_candidate(key)
        self.assertTrue(result['success'])
        from providers.health import record_comment_verification
        directory = self.root / 'health'
        record_verification(key, result['settings'], Collection(**result['collection']), directory=directory)
        record_comment_verification(key, result['settings'], result['comments'], directory=directory, stats=result['comment_stats'])
        evidence = read_verification(key, result['settings'], directory=directory)
        self.assertFalse(evidence['ready'])
        self.assertFalse(evidence['replies_verified'])

    def test_nonempty_wrong_interactive_identity_is_rejected_before_export(self):
        setup = self.setup_candidate()
        setup[2]._profile.side_effect = lambda: {'verified_account_id': 'different-nonempty-account'}
        with self.worker(setup) as (key, interactive, portable), self.assertRaisesRegex(ProviderError, 'identity_mismatch'):
            authorization_worker.validate_candidate(key)
        interactive.browser.export_session.assert_not_called()
        portable.collect.assert_not_called()

    def test_nonempty_wrong_portable_identity_is_rejected_after_export_before_comments(self):
        setup = self.setup_candidate('xiaohongshu', identity='different-restored-account')
        with self.worker(setup) as (key, interactive, portable), self.assertRaisesRegex(ProviderError, 'identity_mismatch'):
            authorization_worker.validate_candidate(key)
        interactive.browser.export_session.assert_called_once_with()
        portable.collect.assert_called_once_with(max_pages=200)
        portable.comments.assert_not_called()

    def test_xiaohongshu_native_identity_uses_public_provided_id_instead_of_internal_uid(self):
        setup = self.setup_candidate('xiaohongshu', identity='public-red-id')
        setup[1].update(platform_uid='internal-object-id', provided_id='public-red-id')
        setup[2]._profile.side_effect = lambda: {'verified_account_id': 'public-red-id'}
        with self.worker(setup) as (key, _, portable):
            result = authorization_worker.validate_candidate(key)
        self.assertTrue(result['success'])
        self.assertEqual('public-red-id', result['collection']['profile']['verified_account_id'])
        portable.comments.assert_called_once()
        mapped = {'provider': 'browser'}
        self.assertEqual('internal-object-id', authorization_worker.expected_identity(setup[1], mapped))
