#!/usr/bin/env python3
"""Real Docker authorization lifecycle against a simulated, fully isolated platform.

No real platform login is exercised. A test-only sitecustomize is bind-mounted,
never added to a production image. Only the unique fixture login service starts.
"""
import argparse
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
KEY = 'baijiahao:国科安芯'
OTHER = 'baijiahao:fixture-unrelated'


def smoke(project, image, docker_config=None):
    if not re.fullmatch(r'promotion-auth-flow-[a-z0-9][a-z0-9-]{0,45}', project):
        raise ValueError('Only a unique promotion-auth-flow-* project is allowed')
    if not image.startswith('promotion-') or image.endswith(':latest'):
        raise ValueError('Use an explicitly tagged disposable promotion-* login image')
    env = dict(os.environ)
    if docker_config:
        env['DOCKER_CONFIG'] = str(Path(docker_config).resolve())
    existing = subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project], env=env, text=True)
    if existing.strip():
        raise ValueError('Existing fixture containers found; choose a new unique project')
    folder = ROOT / '.runtime' / project
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    paths = {name: folder / name for name in ('auth', 'providers', 'sessions', 'runtime', 'data', 'config', 'python-fixture', 'control')}
    for path in paths.values():
        path.mkdir(mode=0o700)
    shutil.copyfile(ROOT / 'tests/fixtures/authorization_sitecustomize.py', paths['python-fixture'] / 'sitecustomize.py')
    catalog = json.loads((ROOT / 'config/article_accounts.json').read_text())['accounts']
    target = next(account for account in catalog if account['platform'] + ':' + account['account_name'] == KEY)
    other_account = {**target, 'account_name': 'fixture-unrelated', 'platform_uid': 'fixture-unrelated-id'}
    (paths['config'] / 'accounts.json').write_text('{"accounts":[]}')
    (paths['config'] / 'article_accounts.json').write_text(json.dumps({'accounts': [target, other_account]}, ensure_ascii=False))
    username, password = 'fixture-operator', secrets.token_urlsafe(28)
    init = subprocess.run([sys.executable, str(ROOT / 'scripts/init_authorization.py'), '--username', username,
                           '--password-stdin', '--auth-dir', str(paths['auth']), '--provider-dir', str(paths['providers']),
                           '--sessions-dir', str(paths['sessions'])], input=password + '\n', text=True, capture_output=True)
    if init.returncode:
        raise RuntimeError('Fixture administrator initialization failed; password was not logged')
    config_file = paths['providers'] / 'providers.json'
    config_file.write_text(json.dumps({'schema_version': 1, 'accounts': {OTHER: {'provider': 'browser', 'fixture_marker': 'untouched'}}}))
    config_file.chmod(0o600)
    other_session = paths['sessions'] / (hashlib.sha256(OTHER.encode()).hexdigest()[:24] + '.storage.json')
    other_session.write_text('{"cookies":[],"origins":[],"fixture":"untouched"}')
    other_session.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    volumes = [{'type': 'bind', 'source': str(paths[name]), 'target': destination, 'read_only': readonly}
               for name, destination, readonly in (
                   ('auth', '/run/promotion/authorization', True), ('providers', '/run/promotion/providers', False),
                   ('sessions', '/app/data/sessions', False), ('runtime', '/app/.runtime', False), ('data', '/app/data', False),
                   ('config', '/app/config', True), ('python-fixture', '/fixture-python', True), ('control', '/fixture-control', False))]
    compose = {'name': project, 'services': {'login': {'image': image, 'init': True, 'shm_size': '1gb',
        'ports': [f'127.0.0.1:{port}:18762'], 'dns': ['127.0.0.1'],
        'extra_hosts': ['baijiahao.baidu.com:127.0.0.1'], 'security_opt': ['no-new-privileges:true'], 'volumes': volumes,
        'environment': {'PROMOTION_TEST_MODE': '1', 'PROMOTION_SIMULATED_PLATFORM': '1',
            'PYTHONPATH': '/fixture-python:/app/pipeline', 'PROMOTION_LOGIN_MODE': 'remote',
            'PROMOTION_BROWSER_CHANNEL': 'chromium', 'PROMOTION_PANEL_ORIGIN': origin,
            'PROMOTION_AUTH_FILE': '/run/promotion/authorization/admin.htpasswd',
            'PROMOTION_RUNTIME_DIR': '/app/.runtime', 'PROMOTION_DATA_DIR': '/app/data',
            'PROMOTION_SESSION_DIR': '/app/data/sessions', 'PROMOTION_PROVIDER_CONFIG': '/run/promotion/providers/providers.json'}}},
        'networks': {'default': {}}}
    compose_path = folder / 'compose.fixture.json'
    compose_path.write_text(json.dumps(compose, ensure_ascii=False, indent=2))
    command = ['docker', 'compose', '--env-file', os.devnull, '-p', project, '-f', str(compose_path)]
    report = {'simulated_platform': True, 'real_platform_docker_authorization_tested': False,
              'project': project, 'image': image, 'gateway': origin, 'artifacts': str(folder), 'checks': {}}
    started = False
    basic = 'Basic ' + base64.b64encode((username + ':' + password).encode()).decode()
    csrf = None
    key_hash = hashlib.sha256(KEY.encode()).hexdigest()[:24]
    session_file = paths['sessions'] / (key_hash + '.storage.json')
    auth_file = paths['sessions'] / 'authorization' / (key_hash + '.json')
    other_before = other_session.read_bytes()

    def expect(name, condition):
        report['checks'][name] = bool(condition)
        if not condition:
            raise RuntimeError('Simulated authorization check failed: ' + name)

    def run(args, *, input=None, check=True, log=None):
        result = subprocess.run(command + args, env=env, input=input, capture_output=True, text=True)
        if log:
            (folder / log).write_text((result.stdout + result.stderr).replace(password, '[redacted]').replace(basic, '[redacted]'))
        if check and result.returncode:
            # Never serialize process input, environment, credentials or session state.
            raise RuntimeError('Isolated Docker fixture command failed; inspect sanitized test artifacts')
        return result

    def request(path, body=None, *, websocket=False):
        headers = {'Authorization': basic}
        if body is not None:
            headers.update({'Origin': origin, 'X-CSRF-Token': csrf or '', 'Content-Type': 'application/json'})
        if websocket:
            headers.update({'Origin': origin, 'Upgrade': 'websocket', 'Connection': 'Upgrade',
                            'Sec-WebSocket-Version': '13', 'Sec-WebSocket-Key': base64.b64encode(secrets.token_bytes(16)).decode(),
                            'Sec-WebSocket-Protocol': 'binary'})
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=8)
        try:
            connection.request('POST' if body is not None else 'GET', path,
                               body=json.dumps(body) if body is not None else None, headers=headers)
            response = connection.getresponse()
            if response.status == 101:
                frame = response.fp.read(2)
                payload = response.fp.read(frame[1]) if len(frame) == 2 and frame[0] & 15 == 2 and frame[1] <= 125 else b''
            else:
                payload = response.read()
            return response.status, payload
        finally:
            connection.close()

    def api(path, body=None):
        status, payload = request(path, body)
        if status != 200:
            raise RuntimeError('Fixture API failed: ' + path + ' status=' + str(status))
        return json.loads(payload)

    def ready():
        nonlocal csrf
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                status, page = request('/')
                match = re.search(r'const csrf\s*=\s*"([^"]+)"', page.decode())
                if status == 200 and match:
                    csrf = match.group(1); return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.5)
        raise RuntimeError('Fixture login gateway did not become ready within 90 seconds')

    def exec_python(source):
        return run(['exec', '-T', 'login', 'python', '-c', source])

    def web_snapshot_hashes():
        source = """import hashlib,json
from pathlib import Path
folders=['/app/web/data','/app/web/articles/data','/app/web/comments/data']
print(json.dumps({str(path):hashlib.sha256(path.read_bytes()).hexdigest() for folder in folders for path in Path(folder).rglob('*') if path.is_file()}))
"""
        return json.loads(exec_python(source).stdout.strip())

    def read_with_real_portable_browser():
        source = '''import json
from provider_setup import accounts
from providers.registry import ProviderRegistry
from providers.base import ProviderError
try:
 result=ProviderRegistry().get(accounts()["baijiahao:国科安芯"]).collect()
 print(json.dumps({"success":True,"simulated_platform":True,"count":len(result.records),"identity_verified":bool(result.profile.get("verified_account_id"))}))
except ProviderError as error:
 print(json.dumps({"success":False,"simulated_platform":True,"reason":error.reason}))
'''
        return json.loads(exec_python(source).stdout.strip())

    def simulate_login():
        source = '''import json,time
from pathlib import Path
from playwright.sync_api import sync_playwright
from sitecustomize import install_routes,FIXTURE_URL
from provider_setup import accounts
active=json.loads(Path('/app/.runtime/authorizations/active.json').read_text())
args=Path('/proc/'+str(active['pid'])+'/cmdline').read_bytes().decode().split('\\0')
expected='--user-data-dir=/app/.runtime/authorizations/'+active['operation_id']+'/profile'
assert expected in args
assert '--remote-debugging-address=127.0.0.1' in args
port=next(a.split('=',1)[1] for a in args if a.startswith('--remote-debugging-port='))
with sync_playwright() as driver:
 browser=None
 for attempt in range(30):
  try: browser=driver.chromium.connect_over_cdp('http://127.0.0.1:'+port,timeout=1000);break
  except Exception: time.sleep(.2)
 assert browser is not None
 context=browser.contexts[0]
 install_routes(context,accounts()['baijiahao:国科安芯']['platform_uid'])
 page=context.new_page();page.goto(FIXTURE_URL,wait_until='domcontentloaded')
 page.get_by_role('button',name='模拟登录').click()
 assert page.locator('#fixture-status').inner_text()=='模拟登录完成'
 print(json.dumps({'simulated_platform':True,'clicked_login':True}))
 # Stop the driver without closing the connected browser or its context.
'''
        return json.loads(exec_python(source).stdout.strip())

    def capture_fixture_desktop():
        """Optional browser-harness proof of the protected noVNC desktop."""
        if not shutil.which('browser-harness'):
            report['desktop_screenshot'] = 'browser-harness unavailable'
            report['desktop_browser_verified'] = False
            return
        try:
            from playwright.sync_api import sync_playwright
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); debugging_port = sock.getsockname()[1]
            with sync_playwright() as driver:
                context = driver.chromium.launch_persistent_context(str(folder / 'desktop-observer'),
                    channel='chrome', headless=True, args=[f'--remote-debugging-port={debugging_port}'],
                    http_credentials={'username': username, 'password': password},
                    viewport={'width': 1440, 'height': 1050}, service_workers='block')
                try:
                    output = folder / 'noVNC-simulated-desktop.png'
                    source = """import base64,time
from pathlib import Path
new_tab(DESKTOP_URL)
wait_for_load()
for attempt in range(40):
 if js("document.documentElement.classList.contains('noVNC_connected') && Boolean(document.querySelector('canvas')?.width && document.querySelector('canvas')?.height)"):
  break
 time.sleep(.5)
assert js("document.documentElement.classList.contains('noVNC_connected') && Boolean(document.querySelector('canvas')?.width && document.querySelector('canvas')?.height)"), 'noVNC client did not connect/render the desktop'
time.sleep(1)
Path(OUTPUT_PATH).write_bytes(base64.b64decode(cdp('Page.captureScreenshot', format='png')['data']))
print('simulated_platform=true desktop_screenshot_saved=true')
""".replace('DESKTOP_URL', repr(origin + '/desktop/vnc.html?autoconnect=true&resize=scale&path=desktop/websockify')).replace('OUTPUT_PATH', repr(str(output)))
                    harness_env = {**os.environ, 'BU_NAME': project + '-desktop', 'BH_RECORD': '0',
                                   'BH_AGENT_WORKSPACE': str(folder / 'harness'), 'BU_CDP_URL': f'http://127.0.0.1:{debugging_port}'}
                    result = subprocess.run(['browser-harness'], input=source, text=True, capture_output=True,
                                            env=harness_env, timeout=45)
                    (folder / 'desktop-browser.log').write_text((result.stdout + result.stderr).replace(password, '[redacted]').replace(basic, '[redacted]'))
                    expect('noVNC browser client connected with rendered canvas', result.returncode == 0 and output.exists())
                    report['desktop_screenshot'] = str(output)
                    report['desktop_browser_verified'] = True
                finally:
                    context.close()
        except ImportError:
            report['desktop_screenshot'] = 'host browser automation package unavailable'
            report['desktop_browser_verified'] = False

    def authorize(label):
        before_config = config_file.read_bytes()
        before_session = session_file.read_bytes() if session_file.exists() else None
        response = api('/api/login', {'account': KEY})
        active = response['login_session']
        operation = active['operation_id']
        expect(label + ' single target desktop', active['account'] == KEY and active['state'] == 'awaiting_login')
        expect(label + ' no early provider write', config_file.read_bytes() == before_config)
        expect(label + ' no early session write', (session_file.read_bytes() if session_file.exists() else None) == before_session)
        expect(label + ' clicked simulated login', simulate_login().get('clicked_login'))
        if label == 'initial':
            capture_fixture_desktop()
        api('/api/authorization/complete', {'account': KEY, 'operation_id': operation})
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            active = api('/api/login-session')
            if active is None:
                break
            if active.get('state') == 'error':
                raise RuntimeError('Fixture candidate failed: ' + str(active.get('message', 'unknown')))
            time.sleep(.5)
        else:
            raise RuntimeError('Fixture candidate verification timed out')
        settings = json.loads(config_file.read_text())['accounts'][KEY]
        expect(label + ' portable settings without debug endpoint', settings.get('session_mode') == 'portable' and 'cdp_url' not in settings and 'headed' not in settings)
        expect(label + ' authorized commit marker', json.loads(auth_file.read_text()).get('status') == 'authorized')
        expect(label + ' exported shared session', session_file.is_file() and session_file.stat().st_mode & 0o777 == 0o600)
        expect(label + ' candidate cleaned', not (paths['runtime'] / 'authorizations' / operation).exists())
        expect(label + ' active state cleaned', not (paths['runtime'] / 'authorizations' / 'active.json').exists())
        expect(label + ' unrelated config preserved', json.loads(config_file.read_text())['accounts'].get(OTHER) == {'provider': 'browser', 'fixture_marker': 'untouched'})
        expect(label + ' unrelated session preserved', other_session.read_bytes() == other_before)
        expect(label + ' actual portable restore', read_with_real_portable_browser().get('success'))

    try:
        started = True
        run(['up', '-d', '--no-deps', '--no-build', 'login'], log='startup.log')
        ready()
        ids = run(['ps', '-q']).stdout.strip().splitlines()
        expect('only fixture login service', len(ids) == 1)
        inspection = json.loads(subprocess.check_output(['docker', 'inspect', ids[0]], env=env))[0]
        ports = inspection['HostConfig']['PortBindings']
        expect('only loopback gateway port published', sorted(ports) == ['18762/tcp'] and ports['18762/tcp'][0]['HostIp'] == '127.0.0.1')
        expect('all mounts use isolated fixture directories', all(mount['Type'] == 'bind' and mount['Source'].startswith(str(folder) + '/') for mount in inspection['Mounts']))
        # Docker internal networks suppress port publishing on the tested Colima
        # engine. Keep the gateway reachable while resolving the only fixture
        # platform to loopback and refusing every unrouted browser request.
        expect('external DNS disabled for fixture container', inspection['HostConfig']['Dns'] == ['127.0.0.1'])
        expect('fixture platform hostname forced to loopback', 'baijiahao.baidu.com:127.0.0.1' in inspection['HostConfig']['ExtraHosts'])
        sources = ['pipeline/login_manager.py', 'pipeline/authorization_worker.py', 'pipeline/control_panel.py',
                   'pipeline/providers/browser.py', 'pipeline/providers/authorization.py', 'pipeline/providers/settings.py',
                   'pipeline/provider_setup.py', 'pipeline/runtime.py', 'pipeline/providers/health.py',
                   'pipeline/providers/registry.py', 'web/manage/index.html', 'scripts/init_authorization.py',
                   'docker/login/entrypoint.sh', 'docker/login/configure.py', 'docker/login/healthcheck.py',
                   'docker/login/nginx.conf.template', 'docker/login/desktop.html']
        code = 'import hashlib,json;from pathlib import Path;print(json.dumps({p:hashlib.sha256(Path("/app",p).read_bytes()).hexdigest() for p in ' + repr(sources) + '}))'
        image_hashes = json.loads(exec_python(code).stdout.strip())
        expect('image uses current authorization implementation', image_hashes == {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources})
        web_before = web_snapshot_hashes()
        expect('authenticated noVNC page', request('/desktop/vnc.html')[0] == 200)
        status, greeting = request('/desktop/websockify', websocket=True)
        expect('authenticated noVNC websocket RFB', status == 101 and greeting.startswith(b'RFB '))
        authorize('initial')
        before_expiry = session_file.read_bytes()
        marker = paths['control'] / 'expire'; marker.write_text('simulated expiration')
        expired = read_with_real_portable_browser()
        expect('simulated automatic expiry', expired.get('reason') == 'session_expired')
        expect('expiry requests reauthorization', json.loads(auth_file.read_text()).get('status') == 'reauth_required')
        expect('expiry preserves last exported session', session_file.read_bytes() == before_expiry)
        marker.unlink()
        calls = (paths['control'] / 'profile-calls').read_text()
        guarded = read_with_real_portable_browser()
        expect('automatic collection paused by guard', guarded.get('reason') == 'session_expired' and (paths['control'] / 'profile-calls').read_text() == calls)
        authorize('reauthorization')
        before_restart = session_file.read_bytes()
        run(['restart', 'login'], log='restart.log')
        ready()
        expect('restart has no active desktop', api('/api/login-session') is None)
        expect('restart preserves portable session', session_file.read_bytes() == before_restart)
        expect('restart portable session is readable', read_with_real_portable_browser().get('success'))
        expect('restart preserves unrelated account', other_session.read_bytes() == other_before)
        untouched = ('dashboard_data.json', 'article_dashboard_data.json', 'comment_timeline.json',
                     'comment_state.json', 'comment_alert.json', 'scheduler_state.json', 'run_history.jsonl')
        expect('no data snapshots reminders or scheduler state written', all(not (paths['data'] / name).exists() for name in untouched))
        expect('no runtime dashboard output published', not (paths['runtime'] / 'web').exists())
        web_after = web_snapshot_hashes()
        expect('packaged dashboard snapshots unchanged', web_before == web_after)
        (folder / 'web-snapshot-hashes.json').write_text(json.dumps({'before': web_before, 'after': web_after}, indent=2))
        report['success'] = True
    except Exception as error:
        report.update(success=False, error=str(error).replace(password, '[redacted]').replace(basic, '[redacted]'))
        raise
    finally:
        if started:
            run(['logs', '--no-color', 'login'], check=False, log='container.log')
            run(['down', '--remove-orphans'], check=False, log='cleanup.log')
        (folder / 'smoke-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--docker-config', type=Path)
    args = parser.parse_args()
    try:
        smoke(args.project, args.image, args.docker_config)
    except Exception:
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
