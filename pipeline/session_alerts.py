#!/usr/bin/env python3
"""Send deduplicated authorization alerts, independently of comment preferences."""

import fcntl
import argparse
import json
import os
import uuid
import time
from datetime import datetime, timezone
from pathlib import Path

from runtime import DATA, is_test
from comment_notifications import EMAIL_PATTERN
from provider_setup import accounts
from providers.authorization import AUTHENTICATION_ERRORS, AuthorizationStore
from providers.base import CN_TZ, ProviderError
from providers.credentials import private_json
from send_comment_alerts import connect, make_message, smtp_config


STATE_PATH = DATA / 'session_alert_state.json'


def read_state(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'schema_version': 1, 'accounts': {}}
    except (OSError, ValueError):
        raise RuntimeError('登录提醒状态无法读取，停止发送以避免重复提醒') from None
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or not isinstance(value.get('accounts'), dict)
            or any(not isinstance(row, dict)
                   or any(not isinstance(row.get(field), str)
                          for field in ('id', 'generation', 'detected_at', 'reason', 'sent_at'))
                   for row in value['accounts'].values())):
        raise RuntimeError('登录提醒状态格式错误，停止发送')
    return value


def build_alert(key, account, incident, recipient):
    origin = os.environ.get('PROMOTION_PANEL_ORIGIN', 'http://127.0.0.1:18762').rstrip('/')
    reason = ('会话身份与绑定账号不一致' if incident['reason'] == 'identity_mismatch'
              else '平台会话已失效或需要人工安全验证')
    detected = datetime.fromisoformat(incident['detected_at']).astimezone(CN_TZ).strftime('%Y-%m-%d %H:%M:%S（北京时间）')
    return {'to': recipient, 'subject': f"【登录失效提醒】{account['account_name']}（{account['platform']}）",
            'id': incident['id'],
            'body': f"平台：{account['platform']}\n账号：{account['account_name']}\n"
                    f"账号标识：{key}\n首次检测时间：{detected}\n原因：{reason}\n\n"
                    f"该账号的自动采集已暂停，请打开授权管理页面：{origin}/\n"
                    "选择对应账号，扫码登录后点击“验证并保存”。若为官方令牌方式，请更新凭证并重新验证。\n"
                    "远程部署的回环地址需先通过 SSH 转发访问。操作说明：docs/DOCKER_AUTHORIZATION.md。\n"
                    "重新授权成功后自动解除本次告警；同一次失效不会重复提醒。\n"
                    "若重新授权后仍失效，请由部署维护人员检查该账号配置与授权服务日志。"}


def send_alert(item):
    config, problems = smtp_config()
    if problems:
        raise RuntimeError('SMTP 配置未就绪')
    message = make_message(item, config['from_addr'])
    message['Message-ID'] = f"<{item['id']}@promotion-dashboard.local>"
    with connect(config) as client:
        if client.send_message(message):
            raise RuntimeError('SMTP 拒绝收件人')


def run_once(*, recipient=None, state_path=None, authorization=None, catalog=None, sender=None):
    recipient = (recipient if recipient is not None else os.environ.get('SESSION_ALERT_RECIPIENT', '')).strip()
    report = {'sent': 0, 'pending': 0, 'failed': 0, 'recovered': 0, 'state_errors': 0, 'send_errors': 0}
    if not recipient or recipient.lower() == 'disabled':
        return report
    if len(recipient) > 254 or not EMAIL_PATTERN.fullmatch(recipient):
        raise RuntimeError('SESSION_ALERT_RECIPIENT 必须是单个有效邮箱')
    path = Path(state_path or STATE_PATH)
    authorization = authorization or AuthorizationStore()
    catalog = catalog if catalog is not None else accounts()
    sender = sender or send_alert
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(path)
        incidents = state['accounts']
        ready = []
        for key, account in catalog.items():
            try:
                auth = authorization.read(key)
            except ProviderError:
                report['failed'] += 1
                report['state_errors'] += 1
                continue
            generation = auth.get('authorized_at', '')
            previous = incidents.get(key)
            if previous and (auth['status'] == 'authorized' or previous['generation'] != generation):
                del incidents[key]
                report['recovered'] += 1
            if auth['status'] != 'reauth_required':
                continue
            if auth.get('reason') not in AUTHENTICATION_ERRORS and key not in incidents:
                continue
            if key not in incidents:
                incidents[key] = {'id': uuid.uuid4().hex, 'generation': generation,
                                  'reason': auth['reason'], 'sent_at': '',
                                  'detected_at': datetime.now(timezone.utc).isoformat()}
            if not incidents[key]['sent_at']:
                ready.append((key, account))
        # Persist pending mail before SMTP. The same file also retains dedupe evidence.
        private_json(path, state)
        if not is_test():
            for key, account in ready:
                incident = incidents[key]
                try:
                    # An operator may have recovered the session since the scan.
                    auth = authorization.read(key)
                except ProviderError:
                    report['failed'] += 1
                    report['state_errors'] += 1
                    continue
                if auth['status'] != 'reauth_required' or auth.get('authorized_at', '') != incident['generation']:
                    continue
                try:
                    sender(build_alert(key, account, incident, recipient))
                except Exception as exc:
                    report['failed'] += 1
                    report['send_errors'] += 1
                    report['send_error_type'] = type(exc).__name__
                    # The SMTP account and recipient are shared: avoid repeated
                    # authentication failures within the same cycle.
                    break
                incident['sent_at'] = datetime.now(timezone.utc).isoformat()
                private_json(path, state)
                report['sent'] += 1
        report['pending'] = sum(not row['sent_at'] for row in incidents.values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--loop', action='store_true', help='每5分钟检查一次，供独立提醒容器使用')
    args = parser.parse_args()
    while True:
        try:
            report = run_once()
        except Exception as exc:
            report = {'failed': 1, 'error_type': type(exc).__name__}
        print(json.dumps({'event': 'session_alert_cycle', 'entry_point': 'daemon' if args.loop else 'cli',
                          'checked_at': datetime.now(timezone.utc).isoformat(),
                          'run_id': uuid.uuid4().hex, **report}), flush=True)
        if not args.loop:
            raise SystemExit(1 if report['failed'] else 0)
        time.sleep(300)


if __name__ == '__main__':
    main()
