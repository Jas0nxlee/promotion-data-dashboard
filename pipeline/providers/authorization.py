"""Private, serialized authorization lifecycle shared by workers and collectors."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid

from runtime import SESSIONS
from .base import ProviderError
from .credentials import private_json

AUTHENTICATION_ERRORS = {'session_expired', 'identity_mismatch'}


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class AuthorizationStore:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else SESSIONS / 'authorization'

    def _path(self, key):
        if not isinstance(key, str) or not key:
            raise ProviderError('invalid_account', '授权状态需要明确的账号标识')
        return self.directory / (hashlib.sha256(key.encode()).hexdigest()[:24] + '.json')

    @contextmanager
    def _locked(self, key):
        path = self._path(key)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'a') as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield path
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read_locked(self, path, key):
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {'account_key': key, 'status': 'untracked'}
        except (OSError, ValueError):
            raise ProviderError('authorization_state_error', '授权状态不可读，暂停账号采集') from None
        if (not isinstance(state, dict) or state.get('account_key') != key
                or state.get('status') not in ('authorizing', 'authorized', 'reauth_required')):
            raise ProviderError('authorization_state_error', '授权状态无效，暂停账号采集')
        if state['status'] == 'authorizing':
            deadline = state.get('expires_at_epoch')
            valid = (isinstance(deadline, (int, float)) and not isinstance(deadline, bool)
                     and math.isfinite(deadline))
            if not valid or deadline <= time.time():
                stamp = _iso(time.time())
                state.update(status='reauth_required', reason='authorization_timeout',
                             message='本次授权已超时，请重新开始授权', updated_at=stamp)
                state['last_error'] = {'reason': state['reason'], 'message': state['message'], 'at': stamp}
                private_json(path, state)
        return state

    def read(self, key):
        with self._locked(key) as path:
            return self._read_locked(path, key)

    def begin(self, key, ttl_seconds=1800):
        if (not isinstance(ttl_seconds, (int, float)) or isinstance(ttl_seconds, bool)
                or not math.isfinite(ttl_seconds) or ttl_seconds <= 0):
            raise ProviderError('invalid_authorization_ttl', '授权有效期必须为正数秒')
        with self._locked(key) as path:
            previous = self._read_locked(path, key)
            if previous['status'] == 'authorizing':
                raise ProviderError('authorization_in_progress', '该账号已有正在进行的授权')
            instant = time.time()
            deadline = instant + ttl_seconds
            if not math.isfinite(deadline):
                raise ProviderError('invalid_authorization_ttl', '授权有效期超出范围')
            try:
                expires = _iso(deadline)
            except (OverflowError, OSError, ValueError):
                raise ProviderError('invalid_authorization_ttl', '授权有效期超出范围') from None
            state = {'schema_version': 1, 'account_key': key, 'status': 'authorizing',
                     'operation_id': uuid.uuid4().hex, 'started_at': _iso(instant),
                     'updated_at': _iso(instant), 'expires_at': expires,
                     'expires_at_epoch': deadline, 'reason': '', 'message': ''}
            if previous.get('authorized_at'):
                state['authorized_at'] = previous['authorized_at']
            private_json(path, state)
            return state

    def _finish(self, key, operation_id, status, reason='', message=''):
        with self._locked(key) as path:
            state = self._read_locked(path, key)
            if (not isinstance(operation_id, str) or not operation_id
                    or state.get('operation_id') != operation_id or state['status'] != 'authorizing'):
                raise ProviderError('authorization_conflict', '授权操作已过期或已被替换，请刷新状态')
            stamp = _iso(time.time())
            state.update(status=status, updated_at=stamp, reason=reason, message=message)
            if status == 'authorized':
                state['authorized_at'] = stamp
                state.pop('last_error', None)
            else:
                state['last_error'] = {'reason': reason, 'message': message, 'at': stamp}
            private_json(path, state)
            return state

    def complete(self, key, operation_id):
        return self._finish(key, operation_id, 'authorized')

    def cancel(self, key, operation_id, reason='authorization_cancelled'):
        return self._finish(key, operation_id, 'reauth_required', str(reason), '本次授权已取消，需要重新授权')

    def require_reauthorization(self, key, reason, message=''):
        with self._locked(key) as path:
            state = self._read_locked(path, key)
            stamp = _iso(time.time())
            state.update(schema_version=1, updated_at=stamp)
            state['last_error'] = {'reason': str(reason), 'message': str(message), 'at': stamp}
            if state['status'] != 'authorizing':
                state.update(status='reauth_required', reason=str(reason), message=str(message))
            private_json(path, state)
            return state

    def guard(self, key):
        state = self.read(key)
        if state['status'] == 'authorizing':
            error = ProviderError('authorization_in_progress', '该账号正在人工授权，暂缓自动采集')
        elif state['status'] == 'reauth_required':
            error = ProviderError('session_expired', '该账号需要重新授权，暂缓自动采集')
        else:
            return state
        # No platform request was made. A delayed caller must not reinterpret
        # this guard refusal as a failure of a newer, successfully saved login.
        error.authorization_guard = True
        error.authorization_operation_id = state.get('operation_id')
        raise error
