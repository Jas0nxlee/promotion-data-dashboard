"""Private, locked token renewal. Secrets are never returned by status APIs."""
import hashlib
import json
import os
import time
import fcntl
import tempfile
from pathlib import Path
from runtime import SESSIONS
from .base import ProviderError


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


class WeChatToken:
    def __init__(self, settings, http, directory=None, clock=None):
        self.settings, self.http = settings, http
        self.directory = Path(directory or SESSIONS / "credentials")
        self.clock = clock or time.time

    def get(self):
        configured = os.environ.get(self.settings.get("access_token_env", ""), "").strip()
        if configured:
            return configured
        app_id = os.environ.get(self.settings.get("app_id_env", ""), "").strip()
        secret = os.environ.get(self.settings.get("app_secret_env", ""), "").strip()
        if not app_id or not secret:
            raise ProviderError("session_expired", "请配置该公众号的官方令牌或 AppID/AppSecret 环境变量")
        if app_id != self.settings.get("expected_app_id"):
            raise ProviderError("identity_mismatch", "AppID 与此账号绑定不一致")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = hashlib.sha256(app_id.encode()).hexdigest()[:24]
        path = self.directory / f"wechat-{name}.json"
        with path.with_suffix(".lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                cached = json.loads(path.read_text())
            except (OSError, ValueError):
                cached = {}
            if cached.get("access_token") and float(cached.get("expires_at", 0)) > self.clock() + 300:
                return cached["access_token"]
            response = self.http.request("POST", "https://api.weixin.qq.com/cgi-bin/stable_token", json={
                "grant_type": "client_credential", "appid": app_id, "secret": secret,
                "force_refresh": False})
            code = response.get("errcode", 0)
            if code == 40164:
                raise ProviderError("permission_denied", "微信要求将采集服务器出口 IP 加入该账号后台白名单")
            if code == 89503:
                raise ProviderError("approval_pending", "微信要求公众号管理员确认本次接口接入")
            if code in (89506, 89507):
                raise ProviderError("permission_denied", "公众号管理员已拒绝此出口的接入，暂停续期")
            if code in (45009, 45011):
                raise ProviderError("rate_limited", "微信凭证接口限流，暂停续期")
            if not response.get("access_token") or not isinstance(response.get("expires_in"), int) or isinstance(response.get("expires_in"), bool):
                raise ProviderError("credential_error", "微信稳定令牌申请失败，请核查接口权限与凭证")
            if response["expires_in"] <= 0:
                raise ProviderError("credential_error", "微信令牌有效期无效")
            private_json(path, {"access_token": response["access_token"], "expires_at": self.clock() + response["expires_in"]})
            return response["access_token"]
