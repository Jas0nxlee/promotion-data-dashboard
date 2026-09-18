import io
import json
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import control_panel
import provider_setup
import fetch_article_data
from providers.base import ProviderError
from providers.public_articles import PUBLIC_ARTICLE_PLATFORMS, public_article_settings
from providers.settings import SettingsStore


class PublicArticlePanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='promotion-public-panel-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SettingsStore(self.root / 'providers.json')
        self.all_accounts = {
            f'{platform}:fixture': {'platform': platform, 'account_name': 'fixture', 'platform_uid': 'canonical'}
            for platform in (*control_panel.NATIVE_PROVIDERS, *PUBLIC_ARTICLE_PLATFORMS)
        }

    def panel(self):
        stack = [patch.object(control_panel, 'SettingsStore', return_value=self.store),
                 patch.object(control_panel, 'accounts', return_value=self.all_accounts),
                 patch.object(control_panel, 'read_verification', return_value={'ready': False}),
                 patch.object(control_panel, 'ThreadPoolExecutor',
                              return_value=SimpleNamespace(submit=lambda callback: callback()))]
        for item in stack:
            item.start()
            self.addCleanup(item.stop)
        return control_panel.Panel()

    def test_real_inventory_lists_all_24_including_7_public_accounts(self):
        self.assertEqual(17, len(provider_setup.accounts()))
        all_accounts = provider_setup.accounts(include_public=True)
        self.assertEqual(24, len(all_accounts))
        self.assertEqual(7, sum(a['platform'] in PUBLIC_ARTICLE_PLATFORMS for a in all_accounts.values()))

    def test_summary_distinguishes_public_routes_without_changing_settings(self):
        panel = self.panel()
        panel.store.update('csdn:fixture', {'provider': 'browser', 'cdp_url': 'http://127.0.0.1:62222'})
        before = self.store.path.read_bytes()
        rows = {row['key']: row for row in panel.summary()}
        control_panel.accounts.assert_called_with(include_public=True)
        for key, account in self.all_accounts.items():
            public = account['platform'] in PUBLIC_ARTICLE_PLATFORMS
            self.assertEqual('public' if public else 'authorized', rows[key]['collection_mode'])
            self.assertEqual(not public, rows[key]['can_login'])
            self.assertEqual(not public, rows[key]['can_configure'])
            if public:
                self.assertTrue(rows[key]['configured'])
                self.assertIn(unittest.mock.call(key, public_article_settings(account)),
                              control_panel.read_verification.call_args_list)
        self.assertEqual(before, self.store.path.read_bytes())

    def test_public_login_and_config_are_rejected_on_server(self):
        panel = self.panel()
        with patch.object(control_panel.subprocess, 'run') as run:
            for platform in PUBLIC_ARTICLE_PLATFORMS:
                with self.assertRaisesRegex(ProviderError, 'unsupported_login'):
                    panel.login(f'{platform}:fixture')
            run.assert_not_called()
        for path in ('/api/config/read', '/api/config/save'):
            body = json.dumps({'account': 'csdn:fixture', 'settings': {'provider': 'browser'}}).encode()
            request = object.__new__(control_panel.handler(panel))
            request.path = path
            request.server = SimpleNamespace(server_port=18761)
            request.headers = {'Host': '127.0.0.1:18761', 'Origin': 'http://127.0.0.1:18761',
                               'X-CSRF-Token': panel.token, 'Content-Length': str(len(body))}
            request.rfile = io.BytesIO(body)
            request.respond = Mock()
            request.do_POST()
            self.assertEqual(400, request.respond.call_args.args[1])
            self.assertIn('unsupported_configuration', request.respond.call_args.args[0]['error'])
        self.assertEqual({}, self.store.read()['accounts'])

    def test_public_probe_skips_browser_setup_and_uses_200_page_isolated_command(self):
        panel = self.panel()
        key = 'toutiao:fixture'
        stale = {'provider': 'wechat_channels_creator', 'session_mode': 'portable',
                 'cdp_url': 'http://127.0.0.1:62222'}
        self.store.update(key, stale)
        before = self.store.path.read_bytes()
        with patch.object(control_panel.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            panel.probe(key)
        self.assertEqual(1, run.call_count)
        command = run.call_args.args[0]
        self.assertEqual('probe', command[2])
        self.assertEqual('200', command[command.index('--max-pages') + 1])
        destination = Path(command[command.index('--output') + 1])
        self.assertEqual(control_panel.ROOT / '.runtime' / 'probes', destination.parent)
        self.assertEqual('1', run.call_args.kwargs['env']['PROMOTION_TEST_MODE'])
        self.assertTrue(panel.jobs[key]['success'])
        self.assertEqual(before, self.store.path.read_bytes())

    def test_probe_job_clears_running_after_subprocess_launch_error(self):
        panel = self.panel()
        with patch.object(control_panel.subprocess, 'run', side_effect=OSError('fixture')):
            panel.probe('csdn:fixture')
        self.assertFalse(panel.jobs['csdn:fixture']['running'])
        self.assertFalse(panel.jobs['csdn:fixture']['success'])

    def test_public_probe_records_raw_metrics_and_closes_each_collector(self):
        entry = {'status': 'partial', 'verified_account_id': 'canonical'}
        records = [{'article_id': '1', 'stats': {'read': None, 'comment': 0}}]
        classes = {'csdn': 'CsdnCollector', 'elecfans': 'ElecfansCollector',
                   'sohu': 'SohuCollector', 'toutiao': 'ToutiaoCollector'}
        for platform, class_name in classes.items():
            account = self.all_accounts[f'{platform}:fixture']
            client, collector = Mock(), Mock()
            collector.collect.return_value = (entry, records)
            with self.subTest(platform=platform), \
                 patch.object(fetch_article_data, 'HttpClient', return_value=client), \
                 patch.object(fetch_article_data, class_name, return_value=collector) as factory, \
                 patch.object(provider_setup, 'record_public_article_verification', return_value={'success': True}) as evidence:
                result = provider_setup.probe_public_article(account, 200)
                evidence.assert_called_once_with(account, entry, records)
                self.assertIs(records, result['records'])
                self.assertIsNone(result['records'][0]['stats']['read'])
                self.assertFalse(result['complete'])
                if platform == 'toutiao':
                    factory.assert_called_once_with(max_pages=200)
                    collector.close.assert_called_once_with()
                else:
                    factory.assert_called_once_with(client, max_pages=200)
                    client.session.close.assert_called_once_with()

    def test_public_failure_updates_health_and_writes_diagnostic_without_snapshot_changes(self):
        account = self.all_accounts['csdn:fixture']
        snapshot = self.root / 'articles.json'
        snapshot.write_text('{"old":"preserved"}')
        monitor = self.root / 'comment_state.json'
        monitor.write_text('{"baseline":1}')
        before = (snapshot.read_bytes(), monitor.read_bytes())
        destination = self.root / '.runtime' / 'probes' / 'csdn.json'
        collector, client = Mock(), Mock()
        collector.collect.side_effect = ProviderError('identity_mismatch', 'fixture owner changed')
        with patch.object(provider_setup, 'ROOT', self.root), \
             patch.object(provider_setup, 'accounts', return_value={'csdn:fixture': account}), \
             patch.object(fetch_article_data, 'HttpClient', return_value=client), \
             patch.object(fetch_article_data, 'CsdnCollector', return_value=collector), \
             patch.object(provider_setup, 'record_public_article_verification', return_value={'success': False}) as evidence, \
             patch.object(sys, 'argv', ['provider_setup.py', 'probe', '--account', 'csdn:fixture',
                                       '--max-pages', '200', '--output', str(destination)]), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ProviderError, 'identity_mismatch'):
                provider_setup.main()
        self.assertEqual('identity_mismatch', evidence.call_args.kwargs['error'].reason)
        result = json.loads(destination.read_text())
        self.assertFalse(result['verification']['success'])
        self.assertEqual('identity_mismatch', result['error']['reason'])
        self.assertEqual(0o600, destination.stat().st_mode & 0o777)
        self.assertEqual(before, (snapshot.read_bytes(), monitor.read_bytes()))
        client.session.close.assert_called_once_with()

    def test_public_cli_exit_reflects_all_readiness_gates_after_diagnostic_write(self):
        import runtime
        import providers.public_articles as public_articles
        script = Path(provider_setup.__file__).resolve()
        config_dir = self.root / 'config'
        config_dir.mkdir()
        account = self.all_accounts['csdn:fixture']
        (config_dir / 'accounts.json').write_text('{"accounts": []}')
        (config_dir / 'article_accounts.json').write_text(json.dumps({'accounts': [account]}))
        complete = {'success': True, 'identity_verified': True, 'contents_complete': True,
                    'metrics_verified': True}
        for failing_gate in ('contents_complete', 'metrics_verified', 'identity_verified', None):
            evidence = dict(complete)
            if failing_gate:
                evidence[failing_gate] = False
            collector, client = Mock(), Mock()
            entry = {'status': 'partial' if failing_gate == 'contents_complete' else 'ok'}
            rows = [{'article_id': '1', 'stats': {'read': None if failing_gate == 'metrics_verified' else 10,
                                                'comment': 0}}]
            collector.collect.return_value = (entry, rows)
            output = self.root / '.runtime' / 'probes' / f'{failing_gate or "complete"}.json'
            with self.subTest(gate=failing_gate), \
                 patch.object(runtime, 'ROOT', self.root), \
                 patch.object(fetch_article_data, 'HttpClient', return_value=client), \
                 patch.object(fetch_article_data, 'CsdnCollector', return_value=collector), \
                 patch.object(public_articles, 'record_public_article_verification', return_value=evidence), \
                 patch.object(sys, 'argv', [str(script), 'probe', '--account', 'csdn:fixture',
                                           '--max-pages', '200', '--output', str(output)]), \
                 redirect_stdout(io.StringIO()):
                exit_code = 0
                try:
                    runpy.run_path(str(script), run_name='__main__')
                except SystemExit as exc:
                    exit_code = exc.code
                self.assertEqual(2 if failing_gate else 0, exit_code)
                saved = json.loads(output.read_text())
                self.assertEqual(evidence, saved['verification'])
                self.assertEqual(rows, saved['records'])
                self.assertEqual(0o600, output.stat().st_mode & 0o777)

    def test_public_reconcile_does_not_require_probe_verification(self):
        import providers.identity as identity
        previous, incoming = self.root / 'previous.json', self.root / 'incoming.json'
        previous.write_text('{"articles": []}')
        incoming.write_text('{"articles": []}')
        output = self.root / '.runtime' / 'probes' / 'reconciled.json'
        reconciled = {'matched': [], 'missing': []}
        with patch.object(provider_setup, 'ROOT', self.root), \
             patch.object(provider_setup, 'accounts', return_value=self.all_accounts), \
             patch.object(identity, 'reconcile_contents', return_value=reconciled) as reconcile, \
             patch.object(provider_setup, 'probe_public_article') as probe, \
             patch.object(sys, 'argv', ['provider_setup.py', 'reconcile', '--account', 'csdn:fixture',
                                       '--previous', str(previous), '--incoming', str(incoming),
                                       '--output', str(output)]), redirect_stdout(io.StringIO()):
            provider_setup.main()
        reconcile.assert_called_once_with([], [], 'article_id')
        probe.assert_not_called()
        self.assertEqual(reconciled, json.loads(output.read_text()))

    def test_baijiahao_uses_authorized_route_and_upgrades_only_placeholder(self):
        from providers.registry import ProviderRegistry
        account = self.all_accounts['baijiahao:fixture']
        placeholder = {'provider': 'browser', 'channel': 'chrome', 'expected_uid': 'configured'}
        settings = control_panel.onboarding_settings(account, placeholder)
        self.assertEqual({**placeholder, 'provider': 'baijiahao_creator'}, settings)
        custom = {**placeholder, 'workflows': {}}
        self.assertEqual(custom, control_panel.onboarding_settings(account, custom))
        self.assertEqual('https://baijiahao.baidu.com/', provider_setup.ENTRIES['baijiahao'])
        self.assertNotIn('baijiahao', PUBLIC_ARTICLE_PLATFORMS)
        factory = Mock(return_value=SimpleNamespace(call_count=0))
        module = SimpleNamespace(BaijiahaoCreatorProvider=factory)
        with patch.dict(sys.modules, {'providers.baijiahao_creator': module}):
            registry = ProviderRegistry({'accounts': {'baijiahao:fixture': settings}})
            self.assertIs(factory.return_value, registry.get(account))
            self.assertIs(factory.return_value, registry.get(account))
        factory.assert_called_once_with(account, settings)
        with self.assertRaisesRegex(ProviderError, 'setup_required'):
            ProviderRegistry({'accounts': {}}).get(account)

    def test_baijiahao_batch_collection_routes_to_provider_without_public_fallback(self):
        account = {**self.all_accounts['baijiahao:fixture'], 'business_line': 'fixture', 'collector': 'baijiahao'}
        config = self.root / 'articles-config.json'
        config.write_text(json.dumps({'accounts': [account]}))
        args = SimpleNamespace(out=self.root / 'snapshot.json', debug=False, public_interval=0.15,
                               manual_input=self.root / 'manual.json', toutiao_pages=200, toutiao_timeout=30,
                               toutiao_headed=False, max_pages=200, wechat_pages=200, only=[])
        client = Mock(call_count=0)
        registry = Mock(call_count=0)
        toutiao = Mock(request_count=0)
        authorized = Mock()
        authorized.collect.return_value = (fetch_article_data.base_account(account), [])
        with patch.object(fetch_article_data, 'CONFIG_PATH', config), \
             patch.object(fetch_article_data, 'HttpClient', return_value=client), \
             patch.object(fetch_article_data, 'ProviderRegistry', return_value=registry), \
             patch.object(fetch_article_data, 'ToutiaoCollector', return_value=toutiao), \
             patch.object(fetch_article_data, 'ProviderArticleCollector', return_value=authorized), \
             patch.object(fetch_article_data, 'BaijiahaoCollector') as public, \
             patch.object(fetch_article_data, 'record_public_article_verification') as public_evidence, \
             redirect_stdout(io.StringIO()):
            result = fetch_article_data.collect(args)
        authorized.collect.assert_called_once_with(account)
        public.assert_not_called()
        public_evidence.assert_not_called()
        self.assertEqual('ok', result['accounts'][0]['status'])
        self.assertFalse(args.out.exists())

    def test_baijiahao_export_preserves_shared_baidu_login_only(self):
        from providers import browser
        account = self.all_accounts['baijiahao:fixture']
        source = browser.BrowserSource(account, {})
        domains = ['.baidu.com', '.baijiahao.baidu.com', '.passport.baidu.com', '.csdn.net',
                   '.baidu.com.example.org', '.evilbaidu.com']
        origins = ['https://baijiahao.baidu.com', 'https://passport.baidu.com', 'https://csdn.net',
                   'https://baidu.com.example.org', 'http://baijiahao.baidu.com']
        source.context = Mock()
        source.context.storage_state.return_value = {
            'cookies': [{'name': 'fixture', 'value': 'test', 'domain': domain} for domain in domains],
            'origins': [{'origin': origin, 'localStorage': []} for origin in origins]}
        with patch.object(browser, 'SESSIONS', self.root):
            result = json.loads(source.export_session().read_text())
        self.assertEqual(domains[:3], [cookie['domain'] for cookie in result['cookies']])
        self.assertEqual(origins[:2], [origin['origin'] for origin in result['origins']])

    def test_baijiahao_health_requires_read_like_comment_but_not_unknown_share(self):
        from providers.base import Collection
        from providers.health import record_verification, read_verification
        key = 'baijiahao:fixture'
        settings = {'provider': 'baijiahao_creator'}
        complete = {'read': 0, 'like': 0, 'comment': 0, 'share': None, 'collect': None}
        for unknown in ('read', 'like', 'comment', None):
            stats = {**complete, **({unknown: None} if unknown else {})}
            collection = Collection({'verified_account_id': 'canonical'}, [{'stats': stats}], source='baijiahao_creator')
            with self.subTest(metric=unknown):
                record_verification(key, settings, collection, directory=self.root)
                result = read_verification(key, settings, directory=self.root)
                self.assertEqual(unknown is None, result['ready'])
                self.assertEqual({'read', 'like', 'comment'}, set(result['metric_coverage']))
