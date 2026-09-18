"""One protected desktop, isolated candidates, and atomic authorization promotion."""
from concurrent.futures import ThreadPoolExecutor
import copy
import fcntl
import base64
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import tempfile

from runtime import ROOT, DATA, SESSIONS
from providers.authorization import AuthorizationStore
from providers.base import Collection, ProviderError
from providers.browser import account_lock, session_key
from providers.credentials import private_json
from providers.health import record_verification, record_comment_verification


class LoginManager:
    def __init__(self, store, catalog, entries, prepare_settings, *, workspace=None,
                 sessions=None, data=None, authorization=None, popen=None, start_reaper=True):
        self.store, self.catalog, self.entries = store, catalog, entries
        self.prepare_settings = prepare_settings
        self.authorization_ttl = int(os.environ.get('PROMOTION_AUTHORIZATION_TTL_SECONDS', '1800'))
        if not 300 <= self.authorization_ttl <= 3600:
            raise ProviderError('invalid_authorization_ttl', '授权窗口时限必须介于300和3600秒')
        self.workspace = Path(workspace or ROOT / '.runtime' / 'authorizations')
        self.sessions, self.data = Path(sessions or SESSIONS), Path(data or DATA)
        self.auth = authorization or AuthorizationStore(self.sessions / 'authorization')
        self.popen = popen or subprocess.Popen
        self.active = None
        self.recovery_required = False
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.stopping = threading.Event()
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.workspace, 0o700)
        self.instance_lock = (self.workspace / 'manager.lock').open('a')
        os.fchmod(self.instance_lock.fileno(), 0o600)
        try:
            fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.instance_lock.close()
            raise ProviderError('authorization_service_busy', '已有授权服务使用此工作目录') from None
        self.state_path = self.workspace / 'active.json'
        self._recover()
        if start_reaper:
            threading.Thread(target=self._reap, daemon=True).start()

    def _recover(self):
        if not self.state_path.exists():
            return
        try:
            old = json.loads(self.state_path.read_text())
            if not isinstance(old, dict):
                old = {}
        except (OSError, ValueError):
            old = {}
        operation = old.get('operation_id', '')
        if re.fullmatch(r'[a-f0-9]{32}', operation) and old.get('account') in self.catalog:
            folder = self.workspace / operation
            # Never trust persisted PIDs alone; bind them to this operation's arguments.
            self._stop_recorded(old.get('worker_pid'), [str(ROOT / 'pipeline/authorization_worker.py'),
                                 '--account', old['account'], '--output', str(folder / 'result.json')])
            self._stop_recorded(old.get('pid'), ['--user-data-dir=' + str(folder / 'profile')])
            with account_lock(old['account']):
                state = self.auth.read(old['account'])
                same = state.get('operation_id') in (None, operation)
                committed = state.get('operation_id') == operation and state['status'] == 'authorized'
                journal = folder / 'promotion-journal.json'
                if journal.exists() and same and not committed:
                    value = json.loads(journal.read_text())
                    if value.get('account') != old['account'] or value.get('operation_id') != operation:
                        raise ProviderError('recovery_failed', '授权恢复记录不匹配，已暂停启动')
                    self._rollback(value)
                if same and not committed:
                    if state['status'] == 'authorizing':
                        self.auth.cancel(old['account'], operation, 'service_restarted')
                    elif state['status'] == 'untracked':
                        self.auth.require_reauthorization(old['account'], 'service_restarted', '授权服务已重启，请重新授权')
            shutil.rmtree(folder, ignore_errors=True)
        self.state_path.unlink(missing_ok=True)

    @staticmethod
    def _stop_recorded(pid, required):
        if not isinstance(pid, int) or pid <= 1:
            return
        try:
            args = Path(f'/proc/{pid}/cmdline').read_bytes().decode().split('\0')
            if all(value in args for value in required) and os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass

    def _persist(self):
        active = self.active
        private_json(self.state_path, {k: active[k] for k in ('account', 'operation_id', 'expires_at')}
                     | {'pid': active['process'].pid,
                        'worker_pid': active['worker'].pid if active.get('worker') else None})

    @staticmethod
    def _backup(path):
        return base64.b64encode(path.read_bytes()).decode('ascii') if path.exists() else None

    @staticmethod
    def _restore(path, saved):
        if saved is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.restore')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(base64.b64decode(saved, validate=True)); stream.flush(); os.fsync(stream.fileno())
            os.replace(name, path); os.chmod(path, 0o600)
        finally:
            Path(name).unlink(missing_ok=True)

    def _rollback(self, journal):
        key = journal['account']
        current = self.store.read()['accounts'].get(key)
        if current == journal['final_settings']:
            self.store.compare_update(key, current, journal['previous_settings'])
        elif current != journal['previous_settings']:
            raise ProviderError('recovery_conflict', '授权恢复发现新的配置改动，请检查后再启动')
        self._restore(self.sessions / (session_key(key) + '.storage.json'), journal['previous_session'])
        self._restore(self.data / 'verification' / (session_key(key) + '.json'), journal['previous_health'])

    @staticmethod
    def _stop(process):
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        except ProcessLookupError:
            pass

    def _cleanup(self):
        if not self.active:
            return
        active = self.active
        self._stop(active.get('worker'))
        self._stop(active.get('process'))
        journal = active['folder'] / 'promotion-journal.json'
        if journal.exists():
            try:
                with account_lock(active['account']):
                    state = self.auth.read(active['account'])
                    same = state.get('operation_id') == active['operation_id']
                    if same and state['status'] != 'authorized':
                        self._rollback(json.loads(journal.read_text()))
            except Exception:
                self.recovery_required = True
                active.update(state='error', desktop_url=None,
                              message='授权保存恢复未完成，请检查存储权限并重启授权服务；原备份已保留')
                return
        self.active = None
        self.state_path.unlink(missing_ok=True)
        folder = active['folder']
        if folder.parent == self.workspace and re.fullmatch(r'[a-f0-9]{32}', folder.name):
            shutil.rmtree(folder, ignore_errors=True)

    def _sweep(self):
        if not self.active or self.recovery_required:
            return
        state = self.auth.read(self.active['account'])
        process = self.active['process']
        same = state.get('operation_id') == self.active['operation_id']
        if (state['status'] != 'authorizing' or not same or process.poll() is not None
                or self.active['expires_at_epoch'] <= time.time()):
            if state['status'] == 'authorizing' and same:
                self.auth.cancel(self.active['account'], self.active['operation_id'], 'browser_closed')
            self._cleanup()

    def _reap(self):
        while not self.stopping.wait(2):
            with self.lock:
                try:
                    self._sweep()
                except Exception:
                    # An unreadable state must not leave an unattended desktop open.
                    self._cleanup()

    def status(self):
        with self.lock:
            self._sweep()
            if not self.active:
                return None
            active = self.active
            return {name: active.get(name) for name in
                    ('account', 'platform', 'operation_id', 'state', 'expires_at', 'message', 'desktop_url')}

    def start(self, key):
        with self.lock:
            self._sweep()
            if self.active:
                if self.active['account'] == key:
                    return self.status()
                raise ProviderError('desktop_busy', '已有账号正在授权，请先完成或取消当前授权')
            account = self.catalog[key]
            if account['platform'] not in self.entries:
                raise ProviderError('unsupported', '该账号使用公开采集，无需登录')
            previous = self.store.read()['accounts'].get(key)
            settings = self.prepare_settings(account, previous or {})
            if settings.get('provider') == 'wechat_official':
                raise ProviderError('unsupported', '此账号使用官方API，请维护其授权配置')
            # Also excludes a collector already running when the operator clicked.
            with account_lock(key):
                state = self.auth.begin(key, ttl_seconds=self.authorization_ttl)
            folder = self.workspace / state['operation_id']
            try:
                folder.mkdir(mode=0o700)
                from playwright.sync_api import sync_playwright
                with sync_playwright() as driver:
                    chrome = driver.chromium.executable_path
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
                profile = folder / 'profile'; profile.mkdir(mode=0o700)
                urls = [self.entries[account['platform']]]
                if account['platform'] == 'xiaohongshu':
                    urls.append('https://www.xiaohongshu.com/')
                args = [chrome, f'--user-data-dir={profile}', f'--remote-debugging-port={port}',
                        '--remote-debugging-address=127.0.0.1', '--no-first-run', '--no-default-browser-check',
                        '--disable-dev-shm-usage', '--lang=zh-CN', '--accept-lang=zh-CN,zh',
                        '--window-size=1360,900', *urls]
                if sys.platform.startswith('linux') and os.geteuid() == 0:
                    args.insert(1, '--no-sandbox')
                process = self.popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
                active = {**state, 'account': key, 'platform': account['platform'], 'state': 'awaiting_login',
                          'desktop_url': '/desktop/vnc.html?autoconnect=true&resize=scale&path=desktop/websockify',
                          'message': '请完成扫码登录，再点击验证并保存', 'folder': folder, 'port': port,
                          'process': process, 'worker': None, 'original': copy.deepcopy(previous), 'settings': settings}
                self.active = active
                self._persist()
                view = self.status()
                if view is None:
                    raise ProviderError('browser_missing', '授权浏览器已退出')
                return view
            except Exception:
                try:
                    self.auth.cancel(key, state['operation_id'], 'browser_start_failed')
                except ProviderError:
                    pass
                self._cleanup()
                shutil.rmtree(folder, ignore_errors=True)
                raise ProviderError('browser_missing', '授权浏览器未能启动，请检查可视化登录服务') from None

    def _operation(self, key, operation):
        self._sweep()
        if self.recovery_required:
            raise ProviderError('recovery_required', '请先修复存储并重启授权服务，待恢复的备份不会被删除')
        if not self.active or self.active['account'] != key or self.active['operation_id'] != operation:
            raise ProviderError('authorization_conflict', '授权已过期或账号不匹配，请刷新后重试')
        return self.active

    def cancel(self, key, operation):
        with self.lock:
            active = self._operation(key, operation)
            if active['state'] == 'verifying':
                raise ProviderError('busy', '正在验证并保存，请等待完成')
            self.auth.cancel(key, operation)
            self._cleanup()
            return {'status': 'cancelled', 'message': '已取消，原有会话文件保持不变；该账号仍待授权'}

    def complete(self, key, operation):
        with self.lock:
            active = self._operation(key, operation)
            if active['state'] == 'verifying':
                raise ProviderError('busy', '正在验证，请勿重复提交')
            active.update(state='verifying', message='正在验证账号、会话恢复和采集范围')
            self.executor.submit(self._verify, key, operation)
            return {'status': 'verifying', 'login_session': self.status()}

    def _verify(self, key, operation):
        try:
            with self.lock:
                active = self._operation(key, operation)
                folder = active['folder']
                if (folder / 'promotion-journal.json').exists():
                    raise ProviderError('recovery_required', '上次保存恢复尚未完成，请检查存储并重启授权服务')
                settings = {**active['settings'], 'channel': 'chromium', 'session_mode': 'interactive',
                            'cdp_url': f"http://127.0.0.1:{active['port']}"}
                config_path, output = folder / 'providers.json', folder / 'result.json'
                private_json(config_path, {'schema_version': 1, 'accounts': {key: settings}})
                env = {**os.environ, 'PROMOTION_TEST_MODE': '1', 'PROMOTION_AUTHORIZATION_OPERATION': '1',
                       'PROMOTION_RUNTIME_DIR': str(folder), 'PROMOTION_DATA_DIR': str(self.data),
                       'PROMOTION_SESSION_DIR': str(folder / 'sessions'), 'PROMOTION_PROVIDER_CONFIG': str(config_path)}
                worker = self.popen([sys.executable, str(ROOT / 'pipeline/authorization_worker.py'), '--account', key,
                                     '--output', str(output)], env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
                active['worker'] = worker
                self._persist()
                timeout = max(1, min(self.authorization_ttl, active['expires_at_epoch'] - time.time()))
            try:
                code = worker.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._stop(worker)
                raise ProviderError('verification_timeout', '验证超时，原有会话未替换，请重新开始') from None
            with self.lock:
                active = self._operation(key, operation)
                try:
                    result = json.loads(output.read_text())
                except (OSError, ValueError):
                    raise ProviderError('verification_failed', '验证未完成，请确认登录后重试') from None
                if code or not result.get('success'):
                    raise ProviderError(result.get('reason', 'verification_failed'), result.get('message', '验证未完成'))
                self._promote(active, result)
                self._cleanup()
        except Exception as exc:
            with self.lock:
                if self.active and self.active['operation_id'] == operation:
                    message = str(exc) if isinstance(exc, ProviderError) else '验证未完成，原有会话未替换，请重试'
                    self.active.update(state='error', message=message)
                    self.active['worker'] = None

    def _promote(self, active, result):
        from authorization_worker import expected_identity
        key = active['account']
        final = copy.deepcopy(result['settings'])
        final.pop('cdp_url', None); final.pop('headed', None)
        final.update(session_mode='portable', channel='chromium')
        if result['collection']['profile'].get('verified_account_id') != expected_identity(self.catalog[key], final):
            raise ProviderError('identity_mismatch', '候选会话的账号与当前目标不一致')
        candidate = active['folder'] / 'sessions' / (session_key(key) + '.storage.json')
        state = json.loads(candidate.read_text())
        if not isinstance(state, dict) or not isinstance(state.get('cookies'), list) or not isinstance(state.get('origins'), list):
            raise ProviderError('invalid_session', '候选会话格式无效，未替换正式会话')
        target = self.sessions / candidate.name
        with account_lock(key):
            auth = self.auth.read(key)
            if auth.get('status') != 'authorizing' or auth.get('operation_id') != active['operation_id']:
                raise ProviderError('authorization_conflict', '授权操作已过期，未保存会话')
            health_path = self.data / 'verification' / (session_key(key) + '.json')
            if self.store.read()['accounts'].get(key) != active['original']:
                raise ProviderError('config_changed', '账号配置已被修改，请取消后重新开始授权')
            journal = {'account': key, 'operation_id': active['operation_id'], 'final_settings': final,
                       'previous_settings': active['original'], 'previous_session': self._backup(target),
                       'previous_health': self._backup(health_path)}
            journal_path = active['folder'] / 'promotion-journal.json'
            private_json(journal_path, journal)
            changed = False
            try:
                self.store.compare_update(key, active['original'], final)
                changed = True
                private_json(target, state)
                collection = Collection(**result['collection'])
                # A new session must not inherit old main-site/reply evidence.
                health_path.unlink(missing_ok=True)
                record_verification(key, final, collection, directory=self.data / 'verification')
                if result.get('comments') is not None:
                    record_comment_verification(key, final, result['comments'], directory=self.data / 'verification',
                                                stats=result.get('comment_stats'))
                self.auth.complete(key, active['operation_id'])
                try:
                    journal_path.unlink(missing_ok=True)
                except OSError:
                    pass  # The authorized operation is the durable commit marker.
            except Exception as exc:
                state = self.auth.read(key)
                if state.get('status') == 'authorized' and state.get('operation_id') == active['operation_id']:
                    return
                if changed or not (isinstance(exc, ProviderError) and exc.reason == 'config_changed'):
                    self._rollback(journal)
                journal_path.unlink(missing_ok=True)
                raise

    def close(self):
        self.stopping.set()
        with self.lock:
            if self.active:
                try:
                    self.auth.cancel(self.active['account'], self.active['operation_id'], 'service_stopped')
                except ProviderError:
                    pass
                self._cleanup()
        self.executor.shutdown(wait=False, cancel_futures=True)
        if not self.instance_lock.closed:
            fcntl.flock(self.instance_lock, fcntl.LOCK_UN)
            self.instance_lock.close()
