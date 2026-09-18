"""Authorization is an explicit CAS lifecycle, never inferred from probe success."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
from providers.authorization import AuthorizationStore
from providers.base import Collection,ProviderError
from providers.browser import BrowserSource,session_key
from providers import health

KEY='zhihu:authorization-fixture'
ACCOUNT={'platform':'zhihu','account_name':'authorization-fixture'}
SETTINGS={'provider':'zhihu_creator','required_metrics':['read','comment']}
GOOD=Collection({'verified_account_id':'fixture'},[{'article_id':'1','stats':{'read':5,'comment':0}}])


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='authorization-state-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.sessions=self.root/'sessions'
        self.store=AuthorizationStore(self.sessions/'authorization')

    def test_missing_state_and_explicit_cas_completion(self):
        self.assertEqual('untracked',self.store.guard(KEY)['status'])
        state=self.store.begin(KEY)
        self.assertEqual('authorizing',state['status'])
        with self.assertRaises(ProviderError) as exc:self.store.guard(KEY)
        self.assertEqual('authorization_in_progress',exc.exception.reason)
        with self.assertRaises(ProviderError):self.store.complete(KEY,'wrong-id')
        done=self.store.complete(KEY,state['operation_id'])
        self.assertEqual('authorized',done['status'])
        self.assertEqual(state['operation_id'],done['operation_id'])
        self.assertEqual('authorized',self.store.guard(KEY)['status'])
        self.assertEqual(0o700,self.store.directory.stat().st_mode&0o777)
        for path in self.store.directory.iterdir():self.assertEqual(0o600,path.stat().st_mode&0o777)

    def test_expired_operation_cannot_complete_or_silently_unblock(self):
        with patch('providers.authorization.time.time',return_value=1000):state=self.store.begin(KEY,ttl_seconds=10)
        with patch('providers.authorization.time.time',return_value=1010):
            self.assertEqual('reauth_required',self.store.read(KEY)['status'])
            with self.assertRaises(ProviderError):self.store.complete(KEY,state['operation_id'])
            with self.assertRaises(ProviderError) as exc:self.store.guard(KEY)
            self.assertEqual('session_expired',exc.exception.reason)
        persisted=json.loads(next(self.store.directory.glob('*.json')).read_text())
        self.assertEqual('authorization_timeout',persisted['reason'])

    def test_cancel_and_stale_worker_cannot_finish_replacement_operation(self):
        first=self.store.begin(KEY)
        self.store.cancel(KEY,first['operation_id'],reason='user_cancelled')
        second=self.store.begin(KEY)
        self.assertNotEqual(first['operation_id'],second['operation_id'])
        for method in (self.store.complete,self.store.cancel):
            with self.assertRaises(ProviderError):method(KEY,first['operation_id'])
        self.assertEqual(second['operation_id'],self.store.read(KEY)['operation_id'])

    def test_error_during_authorizing_is_recorded_without_ending_worker(self):
        state=self.store.begin(KEY)
        result=self.store.require_reauthorization(KEY,'identity_mismatch','wrong account')
        self.assertEqual('authorizing',result['status'])
        self.assertEqual(state['operation_id'],result['operation_id'])
        self.assertEqual(state['expires_at_epoch'],result['expires_at_epoch'])
        self.assertEqual('identity_mismatch',result['last_error']['reason'])
        done=self.store.complete(KEY,state['operation_id'])
        self.assertNotIn('last_error',done)

    def test_parallel_begin_allows_exactly_one_operation(self):
        def begin(_):
            try:return AuthorizationStore(self.store.directory).begin(KEY)
            except ProviderError as exc:return exc.reason
        with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(begin,range(16)))
        winners=[r for r in results if isinstance(r,dict)]
        self.assertEqual(1,len(winners))
        self.assertEqual(15,results.count('authorization_in_progress'))
        self.assertEqual(winners[0]['operation_id'],self.store.read(KEY)['operation_id'])

    def test_parallel_cancel_and_complete_only_one_cas_wins(self):
        state=self.store.begin(KEY)
        def finish(action):
            try:return getattr(AuthorizationStore(self.store.directory),action)(KEY,state['operation_id'])['status']
            except ProviderError as exc:return exc.reason
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(finish,['cancel','complete']))
        self.assertEqual(1,results.count('authorization_conflict'))
        self.assertIn(self.store.read(KEY)['status'],results)

    def test_invalid_ttl_and_corrupt_state_fail_closed(self):
        for ttl in (0,-1,True,float('nan'),float('inf')):
            with self.assertRaises(ProviderError):self.store.begin(KEY,ttl)
        self.store.begin(KEY)
        next(self.store.directory.glob('*.json')).write_text('{broken')
        with self.assertRaises(ProviderError) as exc:self.store.guard(KEY)
        self.assertEqual('authorization_state_error',exc.exception.reason)

    def browser(self):
        (self.sessions/session_key(KEY)).mkdir(parents=True,exist_ok=True)
        driver=Mock();starter=Mock();starter.start.return_value=driver
        source=BrowserSource(ACCOUNT,{'channel':'chromium'})
        return source,driver,starter

    def test_guard_before_lock_and_after_lock_race_prevents_browser_start(self):
        source,driver,starter=self.browser()
        @contextmanager
        def raced_lock(key):
            self.store.begin(key)
            yield
        with patch('providers.browser.SESSIONS',self.sessions),patch.dict(os.environ,{'PROMOTION_AUTHORIZATION_OPERATION':'0'}),patch('providers.browser.account_lock',raced_lock),patch('playwright.sync_api.sync_playwright',return_value=starter) as factory:
            with self.assertRaises(ProviderError) as exc:
                with source.session():pass
            self.assertEqual('authorization_in_progress',exc.exception.reason)
            factory.assert_not_called()
        with patch('providers.browser.SESSIONS',self.sessions),patch('providers.browser.account_lock') as lock,patch.dict(os.environ,{'PROMOTION_AUTHORIZATION_OPERATION':'0'}):
            with self.assertRaises(ProviderError):
                with source.session():pass
            lock.assert_not_called()

    def test_only_exact_explicit_bypass_allows_worker_and_does_not_complete(self):
        state=self.store.begin(KEY)
        source,driver,starter=self.browser()
        with patch('providers.browser.SESSIONS',self.sessions),patch('playwright.sync_api.sync_playwright',return_value=starter):
            for flag in ('0','true',' 1 '):
                with patch.dict(os.environ,{'PROMOTION_AUTHORIZATION_OPERATION':flag}),self.assertRaises(ProviderError):
                    with source.session():pass
            with patch.dict(os.environ,{'PROMOTION_AUTHORIZATION_OPERATION':'1'}):
                with source.session():pass
        driver.chromium.launch_persistent_context.assert_called_once()
        self.assertEqual('authorizing',self.store.read(KEY)['status'])
        self.assertEqual(state['operation_id'],self.store.read(KEY)['operation_id'])

    def test_browser_marks_only_authentication_errors_and_success_does_not_reset(self):
        operation=self.store.begin(KEY);self.store.complete(KEY,operation['operation_id'])
        source,driver,starter=self.browser()
        with patch('providers.browser.SESSIONS',self.sessions),patch('playwright.sync_api.sync_playwright',return_value=starter),patch.dict(os.environ,{'PROMOTION_AUTHORIZATION_OPERATION':'0'}):
            for reason in ('schema_changed','incomplete_pagination','rate_limited'):
                with self.assertRaises(ProviderError):
                    with source.session():raise ProviderError(reason,'fixture')
                self.assertEqual('authorized',self.store.read(KEY)['status'])
            with self.assertRaises(ProviderError):
                with source.session():raise ProviderError('session_expired','fixture')
            self.assertEqual('reauth_required',self.store.read(KEY)['status'])
            with patch.dict(os.environ,{'PROMOTION_AUTHORIZATION_OPERATION':'1'}):
                with source.session():pass
            self.assertEqual('reauth_required',self.store.read(KEY)['status'])

    def test_browser_auth_worker_identity_error_remains_authorizing(self):
        self.store.begin(KEY);source,driver,starter=self.browser()
        with patch('providers.browser.SESSIONS',self.sessions),patch('playwright.sync_api.sync_playwright',return_value=starter),patch.dict(os.environ,{'PROMOTION_AUTHORIZATION_OPERATION':'1'}):
            with self.assertRaises(ProviderError):
                with source.session():raise ProviderError('identity_mismatch','fixture')
        self.assertEqual('authorizing',self.store.read(KEY)['status'])
        self.assertEqual('identity_mismatch',self.store.read(KEY)['last_error']['reason'])

    def test_health_success_never_completes_pending_or_required_authorization(self):
        evidence=self.root/'verification'
        with patch.object(health,'AuthorizationStore',return_value=self.store):
            health.record_verification(KEY,SETTINGS,GOOD,directory=evidence)
            self.assertTrue(health.read_verification(KEY,SETTINGS,directory=evidence)['ready'])
            op=self.store.begin(KEY)
            health.record_verification(KEY,SETTINGS,GOOD,directory=evidence)
            self.assertEqual('authorizing',self.store.read(KEY)['status'])
            self.assertFalse(health.read_verification(KEY,SETTINGS,directory=evidence)['ready'])
            self.store.complete(KEY,op['operation_id'])
            self.assertTrue(health.read_verification(KEY,SETTINGS,directory=evidence)['ready'])
            health.record_verification(KEY,SETTINGS,error=ProviderError('identity_mismatch','fixture'),directory=evidence)
            self.assertEqual('reauth_required',self.store.read(KEY)['status'])
            health.record_verification(KEY,SETTINGS,GOOD,directory=evidence)
            self.assertFalse(health.read_verification(KEY,SETTINGS,directory=evidence)['ready'])
            op=self.store.begin(KEY);self.store.complete(KEY,op['operation_id'])
            self.assertTrue(health.read_verification(KEY,SETTINGS,directory=evidence)['ready'])

    def test_health_non_auth_errors_preserve_state_and_public_collectors_do_not_touch_store(self):
        op=self.store.begin(KEY);self.store.complete(KEY,op['operation_id'])
        with patch.object(health,'AuthorizationStore',return_value=self.store):
            health.record_verification(KEY,SETTINGS,error=ProviderError('schema_changed','fixture'),directory=self.root/'evidence')
            self.assertEqual('authorized',self.store.read(KEY)['status'])
        public={'provider':'csdn_public','required_metrics':['read','comment']}
        with patch.object(health,'AuthorizationStore',side_effect=AssertionError('public has no authorization lifecycle')):
            health.record_verification('csdn:fixture',public,error=ProviderError('identity_mismatch','fixture'),directory=self.root/'public')
            health.record_verification('csdn:fixture',public,GOOD,directory=self.root/'public')
            self.assertTrue(health.read_verification('csdn:fixture',public,directory=self.root/'public')['ready'])


if __name__=='__main__':unittest.main()
