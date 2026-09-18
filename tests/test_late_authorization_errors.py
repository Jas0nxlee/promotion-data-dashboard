"""Late failures cannot invalidate a newer authorization or its health proof."""
from contextlib import contextmanager
import hashlib
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
from providers.browser import BrowserSource,account_lock,session_key
from providers import health

KEY='zhihu:late-error-fixture'
ACCOUNT={'platform':'zhihu','account_name':'late-error-fixture'}
SETTINGS={'provider':'zhihu_creator','required_metrics':['read','comment']}
GOOD=Collection({'verified_account_id':'fixture'},[{'article_id':'1','stats':{'read':5,'comment':0}}])


class LateAuthorizationErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='late-authorization-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.sessions=self.root/'sessions'
        (self.sessions/session_key(KEY)).mkdir(parents=True)
        self.store=AuthorizationStore(self.sessions/'authorization')
        self.evidence=self.root/'evidence'
        self.health_file=self.evidence/(hashlib.sha256(KEY.encode()).hexdigest()[:24]+'.json')
        self.driver=Mock();starter=Mock();starter.start.return_value=self.driver
        for p in (patch('providers.browser.SESSIONS',self.sessions),
                  patch('providers.browser.AuthorizationStore',return_value=self.store),
                  patch.object(health,'AuthorizationStore',return_value=self.store),
                  patch('playwright.sync_api.sync_playwright',return_value=starter),
                  patch.dict(os.environ,{'PROMOTION_AUTHORIZATION_OPERATION':'0'})):
            p.start();self.addCleanup(p.stop)

    def good(self):
        return health.record_verification(KEY,SETTINGS,GOOD,directory=self.evidence)

    def authorize(self,finish=True):
        state=self.store.begin(KEY)
        if finish:self.store.complete(KEY,state['operation_id'])
        return state['operation_id']

    def fail_browser(self,reason='session_expired',worker=False):
        source=BrowserSource(ACCOUNT,{'channel':'chromium'})
        with patch.dict(os.environ,{'PROMOTION_AUTHORIZATION_OPERATION':'1' if worker else '0'}):
            try:
                with source.session():raise ProviderError(reason,'fixture')
            except ProviderError as error:return error
        self.fail('fixture did not raise')

    def late(self,error):
        return health.record_verification(KEY,SETTINGS,error=error,directory=self.evidence)

    def assert_ignored(self,error):
        state=self.store.read(KEY)
        self.good()
        before=self.health_file.read_bytes()
        with patch.object(self.store,'require_reauthorization',side_effect=AssertionError('must not repeat auth transition')):
            self.late(error)
        self.assertEqual(state,self.store.read(KEY))
        self.assertEqual(before,self.health_file.read_bytes())

    def test_guard_refusals_are_marked_and_never_written_after_new_success(self):
        self.store.require_reauthorization(KEY,'session_expired')
        with self.assertRaises(ProviderError) as caught:self.store.guard(KEY)
        refused=caught.exception
        self.assertEqual('session_expired',refused.reason)
        self.assertTrue(refused.authorization_guard)
        self.assertIsNone(refused.authorization_operation_id)
        self.authorize()
        self.assert_ignored(refused)

    def test_authorizing_guard_is_not_a_failed_collection(self):
        operation=self.authorize(finish=False)
        with self.assertRaises(ProviderError) as caught:self.store.guard(KEY)
        self.assertTrue(caught.exception.authorization_guard)
        self.assertEqual(operation,caught.exception.authorization_operation_id)
        self.assertEqual('authorization_in_progress',caught.exception.reason)
        self.assert_ignored(caught.exception)

    def test_browser_auth_transition_recorded_once_inside_account_lock(self):
        operation=self.authorize()
        original=self.store.require_reauthorization
        def locked_transition(*args,**kwargs):
            with self.assertRaises(ProviderError) as busy:
                with account_lock(KEY):pass
            self.assertEqual('session_busy',busy.exception.reason)
            return original(*args,**kwargs)
        with patch.object(self.store,'require_reauthorization',side_effect=locked_transition) as transition:
            error=self.fail_browser('identity_mismatch')
            self.assertTrue(error.authorization_error_recorded)
            self.assertEqual(operation,error.authorization_operation_id)
            before=self.store.read(KEY)
            self.late(error)
            self.assertEqual(1,transition.call_count)
            self.assertEqual(before,self.store.read(KEY))
        self.assertFalse(json.loads(self.health_file.read_text())['success'])

    def test_untracked_error_does_not_overwrite_new_authorizing_or_authorized_generation(self):
        error=self.fail_browser()
        self.assertIsNone(error.authorization_operation_id)
        current=self.authorize(finish=False)
        self.assert_ignored(error)
        self.store.complete(KEY,current)
        self.assert_ignored(error)

    def test_multiple_reauthorization_generations_reject_all_older_errors(self):
        first=self.authorize();old=self.fail_browser()
        second=self.authorize();middle=self.fail_browser('identity_mismatch')
        third=self.authorize(finish=False)
        self.assertNotEqual(first,second);self.assertNotEqual(second,third)
        for error in (old,middle):self.assert_ignored(error)
        self.store.complete(KEY,third)
        for error in (old,middle):self.assert_ignored(error)

    def test_same_operation_retry_success_cannot_be_invalidated_by_old_worker_error(self):
        operation=self.authorize(finish=False)
        error=self.fail_browser(worker=True)
        self.assertEqual(operation,error.authorization_operation_id)
        self.store.complete(KEY,operation)
        self.assert_ignored(error)

    def test_error_evidence_write_is_serialized_with_promotion(self):
        error=self.fail_browser()
        original=health.private_json
        def locked_write(*args,**kwargs):
            with self.assertRaises(ProviderError) as busy:
                with account_lock(KEY):pass
            self.assertEqual('session_busy',busy.exception.reason)
            return original(*args,**kwargs)
        with patch.object(health,'private_json',side_effect=locked_write) as write:self.late(error)
        write.assert_called_once()

    def test_busy_promotion_lock_does_not_receive_late_failure_write(self):
        error=self.fail_browser();self.authorize();self.good()
        before=self.health_file.read_bytes();state=self.store.read(KEY)
        with account_lock(KEY):self.late(error)
        self.assertEqual(before,self.health_file.read_bytes())
        self.assertEqual(state,self.store.read(KEY))

    def test_guard_error_propagated_inside_session_is_not_recorded_as_platform_failure(self):
        self.store.require_reauthorization(KEY,'session_expired')
        with self.assertRaises(ProviderError) as refused:self.store.guard(KEY)
        with patch.dict(os.environ,{'PROMOTION_AUTHORIZATION_OPERATION':'1'}),patch.object(self.store,'require_reauthorization',side_effect=AssertionError('guard is not a platform failure')):
            source=BrowserSource(ACCOUNT,{'channel':'chromium'})
            with self.assertRaises(ProviderError):
                with source.session():raise refused.exception
        self.assertFalse(getattr(refused.exception,'authorization_error_recorded',False))

    def test_non_auth_error_still_records_failure_without_changing_authorization(self):
        self.authorize();before=self.store.read(KEY)
        error=self.fail_browser('schema_changed')
        self.assertFalse(getattr(error,'authorization_error_recorded',False))
        self.good();self.late(error)
        self.assertEqual(before,self.store.read(KEY))
        self.assertFalse(json.loads(self.health_file.read_text())['success'])

    def test_health_marks_previously_unprocessed_auth_error_for_later_deduplication(self):
        self.authorize()
        error=ProviderError('session_expired','fixture without browser transport')
        self.late(error)
        self.assertTrue(error.authorization_error_recorded)
        first=error.authorization_operation_id
        second=self.authorize()
        self.assertNotEqual(first,second)
        self.assert_ignored(error)

    def test_public_auth_named_error_does_not_touch_authorization_or_account_lock(self):
        public={'provider':'csdn_public','required_metrics':['read','comment']}
        with patch.object(health,'AuthorizationStore',side_effect=AssertionError('public has no authorization store')),patch('providers.browser.account_lock',side_effect=AssertionError('public has no browser lease')):
            evidence=health.record_verification('csdn:fixture',public,error=ProviderError('session_expired','public fixture'),directory=self.evidence)
        self.assertFalse(evidence['success'])
        self.assertEqual('session_expired',evidence['reason'])


if __name__=='__main__':unittest.main()
