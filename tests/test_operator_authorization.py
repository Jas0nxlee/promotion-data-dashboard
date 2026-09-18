"""Manual CLI recovery must not take over a worker lease or accept partial proof."""
from contextlib import contextmanager,redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
import provider_setup
from providers import browser as browser_module,health
from providers.authorization import AuthorizationStore
from providers.base import Collection,ProviderError

KEY='zhihu:operator-fixture'
ACCOUNT={'platform':'zhihu','platform_uid':'fixture','account_name':'operator-fixture'}
SETTINGS={'provider':'zhihu_creator','required_metrics':['read','comment']}
GOOD=Collection({'verified_account_id':'fixture'},[{'article_id':'1','stats':{'read':5,'comment':0}}])


class OperatorAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='operator-authorization-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=AuthorizationStore(self.root/'sessions'/'authorization')
        self.output=self.root/'.runtime'/'probes'/'result.json'
        self.real_lock=browser_module.account_lock
        self.browser=SimpleNamespace(settings={})
        self.browser.session=self.browser_session
        self.browser.export_session=Mock(return_value=self.root/'sessions'/'fixture.storage.json')
        self.provider=SimpleNamespace(browser=self.browser,_profile=Mock())
        self.raw_collection=copy.deepcopy(GOOD)
        self.backend_read=Mock()
        self.provider.collect=self.collect
        self.provider.comments=Mock(return_value=([],{'root_pages':1,'reply_pages':0,'comments':0}))
        self.registry=SimpleNamespace(config={'accounts':{KEY:SETTINGS}},get=Mock(return_value=self.provider))
        patches=[patch.object(provider_setup,'ROOT',self.root),
                 patch.object(provider_setup,'accounts',return_value={KEY:ACCOUNT}),
                 patch.object(provider_setup,'AuthorizationStore',return_value=self.store),
                 patch.object(health,'AuthorizationStore',return_value=self.store),
                 patch.object(browser_module,'SESSIONS',self.root/'sessions'),
                 patch.object(provider_setup,'ProviderRegistry',return_value=self.registry),
                 patch.object(provider_setup,'record_verification',side_effect=self.record),
                 patch.object(provider_setup,'record_comment_verification'),
                 patch.dict(os.environ,{'PROMOTION_AUTHORIZATION_OPERATION':'0'})]
        for p in patches:p.start();self.addCleanup(p.stop)

    @contextmanager
    def browser_session(self):
        self.assertEqual('1',os.environ.get('PROMOTION_AUTHORIZATION_OPERATION'))
        with browser_module.account_lock(KEY):yield self.browser

    def collect(self,**kwargs):
        with self.browser.session():
            self.backend_read()
            return self.raw_collection

    def record(self,key,settings,result=None,error=None):
        return health.record_verification(key,settings,result,error,directory=self.root/'evidence')

    def run_cli(self,command='probe'):
        args=['provider_setup.py',command,'--account',KEY,'--output',str(self.output)]
        if command=='probe-comments':args.extend(['--content-id','1'])
        with patch.object(sys,'argv',args),redirect_stdout(io.StringIO()):provider_setup.main()

    def test_active_worker_lease_blocks_every_manual_command_before_side_effects(self):
        state=self.store.begin(KEY)
        for command in provider_setup.OPERATOR_COMMANDS:
            with self.subTest(command=command),self.assertRaises(ProviderError) as exc:self.run_cli(command)
            self.assertEqual('authorization_in_progress',exc.exception.reason)
        self.registry.get.assert_not_called()
        self.assertEqual(state,self.store.read(KEY))
        self.assertEqual('0',os.environ['PROMOTION_AUTHORIZATION_OPERATION'])

    def test_full_probe_recovers_required_state_only_after_output_under_account_lock(self):
        self.store.require_reauthorization(KEY,'session_expired','fixture')
        original_begin=self.store.begin
        def begin_under_lock(*args,**kwargs):
            self.assertTrue(self.output.exists())
            with self.assertRaises(ProviderError) as exc:
                with self.real_lock(KEY):pass
            self.assertEqual('session_busy',exc.exception.reason)
            return original_begin(*args,**kwargs)
        with patch.object(self.store,'begin',side_effect=begin_under_lock):self.run_cli()
        self.assertEqual('authorized',self.store.read(KEY)['status'])
        saved=json.loads(self.output.read_text())
        self.assertTrue(saved['verification']['metrics_verified'])
        self.assertEqual(0o600,self.output.stat().st_mode&0o777)
        self.assertEqual('0',os.environ['PROMOTION_AUTHORIZATION_OPERATION'])
        self.assertIs(self.real_lock,browser_module.account_lock)

    def test_partial_unknown_metrics_or_missing_identity_leave_reauth_and_save_diagnostic(self):
        self.store.require_reauthorization(KEY,'session_expired')
        variants=[Collection(GOOD.profile,GOOD.records,complete=False),
                  Collection(GOOD.profile,[{'article_id':'1','stats':{'read':None,'comment':0}}]),
                  Collection({},GOOD.records)]
        for collection in variants:
            self.raw_collection=collection
            with self.subTest(collection=collection),self.assertRaises(ProviderError) as exc:self.run_cli()
            self.assertEqual('verification_incomplete',exc.exception.reason)
            self.assertEqual('reauth_required',self.store.read(KEY)['status'])
            saved=json.loads(self.output.read_text())
            self.assertFalse(all(saved['verification'][f] for f in ('identity_verified','contents_complete','metrics_verified')))
            self.assertEqual('0',os.environ['PROMOTION_AUTHORIZATION_OPERATION'])

    def test_export_and_comment_probe_can_validate_but_do_not_resume_collection(self):
        self.store.require_reauthorization(KEY,'session_expired')
        self.run_cli('export-session')
        self.browser.export_session.assert_called_once()
        self.assertEqual('reauth_required',self.store.read(KEY)['status'])
        self.run_cli('probe-comments')
        self.provider.comments.assert_called_once()
        self.assertEqual('reauth_required',self.store.read(KEY)['status'])

    def test_explicit_worker_probe_does_not_complete_its_lease(self):
        state=self.store.begin(KEY)
        with patch.dict(os.environ,{'PROMOTION_AUTHORIZATION_OPERATION':'1'}):
            self.run_cli()
            self.assertEqual('1',os.environ['PROMOTION_AUTHORIZATION_OPERATION'])
        self.assertEqual(state,self.store.read(KEY))
        self.assertTrue(json.loads(self.output.read_text())['complete'])

    def test_worker_with_cancelled_state_does_not_recover_without_manager_cas(self):
        self.store.require_reauthorization(KEY,'operator_cancelled')
        with patch.dict(os.environ,{'PROMOTION_AUTHORIZATION_OPERATION':'1'}):self.run_cli()
        self.assertEqual('reauth_required',self.store.read(KEY)['status'])

    def test_lease_started_after_preflight_is_rechecked_before_browser_work(self):
        def racing_provider(account):
            self.store.begin(KEY)
            return self.provider
        self.registry.get.side_effect=racing_provider
        with self.assertRaises(ProviderError) as exc:self.run_cli()
        self.assertEqual('authorization_in_progress',exc.exception.reason)
        self.backend_read.assert_not_called()
        self.assertEqual('authorizing',self.store.read(KEY)['status'])
        self.assertIs(self.real_lock,browser_module.account_lock)

    def test_new_lease_after_successful_collect_is_not_overwritten_by_recovery(self):
        self.store.require_reauthorization(KEY,'session_expired')
        def record_and_start_lease(*args,**kwargs):
            value=self.record(*args,**kwargs)
            self.store.begin(KEY)
            return value
        with patch.object(provider_setup,'record_verification',side_effect=record_and_start_lease):
            with self.assertRaises(ProviderError) as exc:self.run_cli()
        self.assertEqual('authorization_in_progress',exc.exception.reason)
        self.assertEqual('authorizing',self.store.read(KEY)['status'])
        self.assertTrue(self.output.exists())

    def test_successful_untracked_account_does_not_create_authorization_operation(self):
        self.run_cli()
        self.assertEqual('untracked',self.store.read(KEY)['status'])

    def test_public_probe_does_not_read_authorization_or_change_env(self):
        public={**ACCOUNT,'platform':'csdn'}
        key='csdn:operator-fixture'
        evidence={'identity_verified':True,'contents_complete':True,'metrics_verified':True}
        result={'profile':{},'records':[],'complete':True,'verification':evidence}
        with patch.object(provider_setup,'accounts',return_value={key:public}),patch.object(provider_setup,'AuthorizationStore',side_effect=AssertionError('public untouched')),patch.object(provider_setup,'probe_public_article',return_value=result),patch.object(sys,'argv',['provider_setup.py','probe','--account',key,'--output',str(self.output)]),redirect_stdout(io.StringIO()):
            provider_setup.main()
        self.assertEqual('0',os.environ['PROMOTION_AUTHORIZATION_OPERATION'])


if __name__=='__main__':unittest.main()
