"""Operation-local raw publication pages; never cache credentials or settings."""
import copy
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

from .base import ProviderError, now
from .credentials import private_json


def _fail(message):
    raise ProviderError('publication_cache_corrupt', message)


def _object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {'unparsed': value}
        return parsed if isinstance(parsed, dict) else {'unparsed': value}
    return {} if value is None else {'unparsed': value}


def _children(value):
    if value is None:
        return None
    if not isinstance(value, list):
        return {'unparsed': value}
    keys = ('appmsgid', 'app_msg_id', 'article_id', 'msgid', 'itemidx', 'idx',
            'is_deleted', 'item_show_type', 'share_type', 'status', 'create_time',
            'publish_time', 'publish_at')
    return [{k: item.get(k) for k in keys} if isinstance(item, dict) else item for item in value]


def _head_signature(head):
    groups = []
    for group in head['publish_list']:
        if not isinstance(group, dict):
            _fail('发表目录头包含非对象消息组')
        sent, result, published = (_object(group.get(name)) for name in ('sent_info', 'sent_result', 'publish_info'))
        groups.append({
            'msgid': group.get('msgid'), 'type': group.get('type'),
            'publish_type': group.get('publish_type'), 'new_publish': group.get('new_publish'),
            'sent_info': {k: sent.get(k) for k in ('time', 'is_published')},
            'sent_result': {k: result.get(k) for k in ('msg_status', 'msg_fail_reason', 'refuse_reason', 'update_time')},
            'status_label': _object(group.get('view')).get('status'),
            'publish_info': {k: published.get(k) for k in ('publish_status', 'create_time', 'msgid', 'unparsed')},
            'appmsg_info': _children(group.get('appmsg_info')),
            'appmsgex': _children(group.get('appmsgex')),
            'published_appmsg_info': _children(published.get('appmsg_info')),
            'published_appmsgex': _children(published.get('appmsgex')),
        })
    content = {'total_count': head['total_count'], 'count': head['count'], 'groups': groups}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _no_credentials(value):
    forbidden = {'cookie', 'cookies', 'token', 'authorization', 'access_token', 'refresh_token',
                 'password', 'secret', 'config', 'settings', 'storage_state', 'rawkeybuff', 'pluginsessionid'}
    if isinstance(value, dict):
        if any(str(key).lower() in forbidden for key in value):
            _fail('分页缓存仅允许发表记录，不允许凭证或配置字段')
        for child in value.values():
            _no_credentials(child)
    elif isinstance(value, list):
        for child in value:
            _no_credentials(child)


class PublicationPageCache:
    def __init__(self, directory, identity, fresh_head):
        if '://' in str(directory):
            _fail('分页缓存必须使用本地操作目录')
        self.directory = Path(directory)
        if self.directory.is_symlink():
            _fail('分页缓存目录不能是符号链接')
        if (not isinstance(identity, dict) or set(identity) != {'verified_account_id', 'public_biz'}
                or any(not isinstance(v, str) or not v for v in identity.values())):
            _fail('分页缓存身份必须只包含公众号原始ID和公开biz')
        self.identity = dict(identity)
        if not isinstance(fresh_head, dict):
            _fail('发表目录头必须是对象')
        self.total = fresh_head.get('total_count')
        self.count = fresh_head.get('count')
        head = self._page(fresh_head, 0, check_time=False)
        self.signature = _head_signature(head)
        self.manifest = {'version': 1, 'identity': self.identity, 'head_signature': self.signature,
                         'total_count': self.total, 'count': self.count}
        with self._locked():
            previous = self._load(self.directory / 'manifest.json', missing=True)
            if previous is not None and (not isinstance(previous, dict) or previous.get('version') != 1
                                         or not isinstance(previous.get('identity'), dict)
                                         or not isinstance(previous.get('head_signature'), str)):
                _fail('分页缓存索引无效')
            if previous != self.manifest:
                for path in self.directory.glob('page-*.json'):
                    if re.fullmatch(r'page-\d+\.json', path.name):
                        path.unlink()
                private_json(self.directory / 'manifest.json', self.manifest)
            self._write(head)

    @contextmanager
    def _locked(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        fd = os.open(self.directory / 'cache.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'a') as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _load(path, missing=False):
        if path.is_symlink():
            _fail('分页缓存文件不能是符号链接')
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            if missing:
                return None
            _fail('分页缓存索引缺失')
        except (OSError, ValueError):
            _fail('分页缓存文件损坏，停止复用')
        if not isinstance(value, dict):
            _fail('分页缓存文件不是有效对象')
        return value

    def _page(self, data, begin, *, check_time):
        if (not isinstance(data, dict) or type(begin) is not int or begin < 0
                or type(data.get('begin')) is not int or data['begin'] != begin
                or type(self.total) is not int or self.total < 0
                or type(data.get('total_count')) is not int or data['total_count'] != self.total
                or type(self.count) is not int or self.count <= 0
                or type(data.get('count')) is not int or data['count'] != self.count
                or begin > self.total or not isinstance(data.get('publish_list'), list)
                or len(data['publish_list']) != min(self.count, self.total - begin)):
            _fail('分页缓存页码、总数或记录数不一致')
        _no_credentials(data)
        if check_time:
            fetched = data.get('_fetched_at')
            try:
                if not isinstance(fetched, str) or datetime.fromisoformat(fetched.replace('Z', '+00:00')).tzinfo is None:
                    raise ValueError()
            except (TypeError, ValueError):
                _fail('分页缓存获取时间缺失或不是有效ISO时间')
        return copy.deepcopy(data)

    def _check_manifest(self):
        if self._load(self.directory / 'manifest.json') != self.manifest:
            raise ProviderError('publication_cache_changed', '操作目录已切换账号或目录版本，停止复用旧缓存')

    def _write(self, data):
        fresh = copy.deepcopy(data)
        fresh['_fetched_at'] = now()
        private_json(self.directory / f"page-{fresh['begin']}.json", {'identity': self.identity, 'data': fresh})
        return fresh

    def get(self, begin):
        if type(begin) is not int or begin < 0:
            _fail('分页缓存页码必须为非负整数')
        with self._locked():
            self._check_manifest()
            saved = self._load(self.directory / f'page-{begin}.json', missing=True)
            if saved is None:
                return None
            if not isinstance(saved, dict) or saved.get('identity') != self.identity:
                _fail('分页缓存账号身份不一致')
            return self._page(saved.get('data'), begin, check_time=True)

    def put(self, data):
        if not isinstance(data, dict):
            _fail('分页缓存输入必须为对象')
        fresh = self._page(data, data.get('begin'), check_time=False)
        with self._locked():
            self._check_manifest()
            if fresh['begin'] == 0 and _head_signature(fresh) != self.signature:
                raise ProviderError('publication_cache_changed', '新目录头发生变化，请重新初始化分页缓存')
            return self._write(fresh)
