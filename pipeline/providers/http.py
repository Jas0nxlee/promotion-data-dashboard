"""Small, bounded transport restricted to each platform's explicit hosts."""
import time
from urllib.parse import urlparse
import requests
from .base import ProviderError


class Http:
    def __init__(self, hosts, interval=0.6, session=None):
        self.hosts = set(hosts)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        self.interval = interval
        self.last_call = 0
        self.call_count = 0

    def request(self, method, url, *, params=None, json=None, headers=None):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in self.hosts:
            raise ProviderError("invalid_host", "请求不属于当前平台允许的 HTTPS 域名")
        for attempt in range(3):
            time.sleep(max(0, self.interval - (time.monotonic() - self.last_call)))
            self.last_call = time.monotonic()
            self.call_count += 1
            try:
                r = self.session.request(method, url, params=params, json=json, headers=headers,
                                         timeout=20, allow_redirects=False)
            except requests.RequestException:
                if attempt < 2:
                    continue
                # Do not echo request URLs containing credentials.
                raise ProviderError("network_error", "平台连接失败") from None
            if r.status_code in (401, 403):
                raise ProviderError("permission_denied", "平台拒绝访问，请核查账号权限或登录状态")
            if r.status_code in (412, 429):
                raise ProviderError("rate_limited", "平台访问校验未通过，停止当前账号请求")
            if r.status_code >= 500 and attempt < 2:
                continue
            if r.status_code != 200:
                raise ProviderError("http_error", f"平台返回 HTTP {r.status_code}")
            try:
                return r.json()
            except ValueError:
                raise ProviderError("schema_changed", "平台未返回 JSON") from None
        raise ProviderError("network_error", "平台请求未完成")
