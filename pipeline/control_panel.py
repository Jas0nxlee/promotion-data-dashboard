#!/usr/bin/env python3
"""Loopback-only account onboarding panel. Never starts the production scheduler."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
from urllib.parse import urlparse

from runtime import ROOT, SESSIONS, PROVIDER_CONFIG, RUNTIME
from provider_setup import accounts, ENTRIES
from providers.browser import session_key
from providers.settings import SettingsStore
from providers.health import read_verification
from providers.base import ProviderError
from providers.authorization import AuthorizationStore
from providers.public_articles import PUBLIC_ARTICLE_PLATFORMS, public_article_settings
from comment_notifications import NotificationStore, effective_rule
from comment_monitor import load_recipient_map


NATIVE_PROVIDERS = {
    "bilibili": "bilibili_creator", "douyin": "douyin_creator",
    "wechat_channels": "wechat_channels_creator", "xiaohongshu": "xiaohongshu_creator",
    "zhihu": "zhihu_creator",
    "baijiahao": "baijiahao_creator",
    "wechat_service": "wechat_browser", "wechat_subscription": "wechat_browser",
}
# Upgrade only the old login placeholder shape. Unknown fields, even empty
# recipe/mapping fields, may represent a user's unfinished custom configuration.
PLACEHOLDER_FIELDS = {
    "provider", "channel", "cdp_url", "session_mode", "headed", "timeout_ms", "request_interval",
    "expected_uid", "expected_sph", "expected_finder_id", "comment_identity_compatible",
    "replaces_platform_uid", "required_metrics", "required_extra_metrics", "expected_biz", "last_verification",
}


def onboarding_settings(account, settings):
    """Promote untouched login placeholders only during explicit onboarding."""
    result = dict(settings)
    native = NATIVE_PROVIDERS.get(account["platform"])
    if native and result.get("provider", "browser") == "browser" and not (result.keys() - PLACEHOLDER_FIELDS):
        result["provider"] = native
    result.setdefault("provider", "browser")
    if result["provider"] == "bilibili_creator":
        result.setdefault("expected_uid", str(account["platform_uid"]))
    elif result["provider"] == "wechat_channels_creator":
        result.setdefault("expected_sph", str(account["platform_uid"]))
    return result


class Panel:
    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.store = SettingsStore()
        self.notifications = NotificationStore()
        self.jobs = {}
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.lock = threading.Lock()
        self.authorization = AuthorizationStore()
        self.login_manager = None
        if os.environ.get("PROMOTION_LOGIN_MODE") == "remote":
            from login_manager import LoginManager
            self.login_manager = LoginManager(self.store, accounts(include_public=True), ENTRIES, onboarding_settings)

    def summary(self):
        config = self.store.read()["accounts"]
        notification_rules = self.notifications.read()
        platform_recipients, _ = load_recipient_map()
        result = []
        active = self.login_manager.status() if self.login_manager else None
        for key, account in accounts(include_public=True).items():
            public = account["platform"] in PUBLIC_ARTICLE_PLATFORMS
            settings = public_article_settings(account) if public else config.get(key, {})
            try:
                health = read_verification(key, settings)
                state = self.authorization.read(key) if not public else {"status": "not_required"}
                if state["status"] == "untracked":
                    state = {"status": "authorized" if health.get("ready") else "unverified" if settings else "unconfigured"}
            except ProviderError:
                health = {"ready": False, "status": "attention"}
                state = {"status": "reauth_required", "message": "授权状态无法读取，请检查状态文件"}
            result.append({"key": key, "name": account["account_name"], "platform": account["platform"],
                           "configured": bool(settings), "collection_mode": "public" if public else "authorized",
                           "can_login": not public and settings.get("provider") != "wechat_official", "can_configure": not public,
                           "authorization": {name: state.get(name) for name in ("status", "message", "expires_at", "updated_at")},
                           "login_session": active if active and active["account"] == key else None,
                           "health": health,
                           "notification": {**effective_rule(key, account["platform"], notification_rules, platform_recipients),
                                            "configured_email": notification_rules.get(key, {}).get("email", ""),
                                            "configured_owner": notification_rules.get(key, {}).get("owner", "")},
                           "job": self.jobs.get(key, {})})
        return result

    def login(self, key):
        account = accounts(include_public=True)[key]
        if account["platform"] in PUBLIC_ARTICLE_PLATFORMS:
            raise ProviderError("unsupported_login", "此平台使用公开采集，后台登录尚未接入")
        if self.login_manager:
            return {"status": "login_opened", "login_session": self.login_manager.start(key)}
        profile = SESSIONS / session_key(key)
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = self.store.read()["accounts"].get(key, {})
        port = None
        if current.get("cdp_url"):
            parsed = urlparse(current["cdp_url"])
            if parsed.hostname in {"127.0.0.1", "localhost"}:
                try:
                    with socket.create_connection((parsed.hostname, parsed.port), timeout=0.2):
                        port = parsed.port
                except OSError:
                    pass
        if port is None:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
        args = [f"--user-data-dir={profile}", f"--remote-debugging-port={port}", "--no-first-run", "--no-default-browser-check", ENTRIES[account["platform"]]]
        if sys.platform == "darwin":
            subprocess.run(["open", "-na", "Google Chrome", "--args", *args], check=True)
        else:
            import shutil
            chrome = shutil.which("google-chrome") or shutil.which("chromium")
            if not chrome:
                raise ProviderError("browser_missing", "请安装可见桌面 Chrome/Chromium 后登录")
            subprocess.Popen([chrome, *args], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        current = onboarding_settings(account, current)
        current.update({"channel": "chrome", "cdp_url": f"http://127.0.0.1:{port}"})
        current.setdefault("comment_identity_compatible", False)
        self.store.update(key, current)
        return {"status": "login_opened", "message": "请在独立浏览器扫码；尚未认定登录或采集成功"}

    def probe(self, key):
        account = accounts(include_public=True)[key]
        public = account["platform"] in PUBLIC_ARTICLE_PLATFORMS
        if not public and self.authorization.read(key)["status"] == "authorizing":
            raise ProviderError("authorization_in_progress", "该账号正在授权，请使用验证并保存按钮")
        with self.lock:
            if self.jobs.get(key, {}).get("running"):
                raise ProviderError("busy", "该账号正在验证")
            self.jobs[key] = {"running": True, "message": "正在只读验证"}

        def execute():
            destination = ROOT / ".runtime" / "probes" / (session_key(key) + ".json")
            env = {**os.environ, "PROMOTION_TEST_MODE": "1",
                   "PROMOTION_RUNTIME_DIR": str(RUNTIME if RUNTIME != ROOT else ROOT / ".runtime" / "local"),
                   "PROMOTION_SESSION_DIR": str(SESSIONS), "PROMOTION_PROVIDER_CONFIG": str(PROVIDER_CONFIG)}
            try:
                settings = self.store.read()["accounts"].get(key, {})
                upgraded = settings if public else onboarding_settings(account, settings)
                if upgraded != settings:
                    settings = self.store.update(key, upgraded)
                if not public and settings.get("provider") == "wechat_channels_creator" and not settings.get("expected_finder_id"):
                    binding = subprocess.run([sys.executable, str(ROOT / "pipeline/provider_setup.py"), "bind-channels", "--account", key],
                                             env=env, capture_output=True, text=True, timeout=120)
                    if binding.returncode:
                        self.jobs[key] = {"running": False, "success": False, "message": "身份绑定未完成，请确认已登录且视频号短号与配置一致"}
                        return
                    settings = self.store.read()["accounts"].get(key, {})
                if not public and settings.get("session_mode") == "portable" and settings.get("cdp_url"):
                    exported = subprocess.run([sys.executable, str(ROOT / "pipeline/provider_setup.py"), "export-session", "--account", key],
                                              env=env, capture_output=True, text=True, timeout=120)
                    if exported.returncode:
                        self.jobs[key] = {"running": False, "success": False, "message": "登录会话更新未完成，请确认独立浏览器中的账号身份"}
                        return
                result = subprocess.run([sys.executable, str(ROOT / "pipeline/provider_setup.py"), "probe", "--account", key,
                                         "--max-pages", "200", "--output", str(destination)], env=env,
                                        capture_output=True, text=True, timeout=1800)
                message = "验证结果已保存；以覆盖检查为准" if result.returncode == 0 else ("公开采集未完成，请检查平台响应与覆盖状态" if public else "未完成，请检查登录状态与采集配置")
                self.jobs[key] = {"running": False, "success": result.returncode == 0, "message": message}
            except subprocess.TimeoutExpired:
                self.jobs[key] = {"running": False, "success": False, "message": "验证超时，未更新正式数据"}
            except Exception:
                self.jobs[key] = {"running": False, "success": False, "message": "验证未完成，请检查本地环境"}
        self.executor.submit(execute)
        return {"status": "running"}

    def close(self):
        if self.login_manager:
            self.login_manager.close()
        self.executor.shutdown(wait=False, cancel_futures=True)


def configured_origins():
    values = [os.environ.get("PROMOTION_PANEL_ORIGIN", ""),
              *os.environ.get("PROMOTION_PANEL_ALLOWED_ORIGINS", "").split(",")]
    origins = set()
    for value in values:
        origin = value.strip()
        if not origin:
            continue
        parsed = urlparse(origin)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("授权管理访问地址端口无效") from None
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path
                or parsed.params or parsed.query or parsed.fragment or parsed.username or parsed.password
                or not re.fullmatch(r"[A-Za-z0-9.\[\]:-]+", parsed.netloc)
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError("授权管理访问地址必须是准确的 http(s)://主机[:端口]，不能含路径、凭证或通配符")
        origins.add(origin)
    return origins


def handler(panel):
    external_origins = configured_origins()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, value, code=200):
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def host_allowed(self):
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            hosts.update(urlparse(origin).netloc for origin in external_origins)
            return self.headers.get("Host") in hosts

        def origin_allowed(self):
            origins = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            origins.update(external_origins)
            origin = self.headers.get("Origin", "")
            return origin in origins and urlparse(origin).netloc == self.headers.get("Host")

        def do_GET(self):
            if not self.host_allowed():
                return self.respond({"error": "invalid host"}, 403)
            if self.path == "/":
                html = (ROOT / "web/manage/index.html").read_text().replace("__CSRF_TOKEN__", panel.token)
                data = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif self.path == "/api/accounts":
                self.respond(panel.summary())
            elif self.path == "/api/login-session":
                self.respond(panel.login_manager.status() if panel.login_manager else None)
            else:
                self.respond({"error": "not found"}, 404)

        def do_POST(self):
            if not self.host_allowed() or self.headers.get("X-CSRF-Token") != panel.token or not self.origin_allowed():
                return self.respond({"error": "页面会话已更新或请求来源不符，请刷新本页面后重试"}, 403)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 200000:
                    return self.respond({"error": "invalid body size"}, 413)
                payload = json.loads(self.rfile.read(size))
                key = payload["account"]
                all_accounts = accounts(include_public=True)
                if key not in all_accounts:
                    raise ProviderError("unknown_account", "账号不在本项目清单内")
                if (all_accounts[key]["platform"] in PUBLIC_ARTICLE_PLATFORMS
                        and self.path in {"/api/config/read", "/api/config/save"}):
                    raise ProviderError("unsupported_configuration", "此平台使用公开采集，后台配置尚未接入")
                if self.path == "/api/login":
                    result = panel.login(key)
                elif self.path in {"/api/authorization/complete", "/api/authorization/cancel"}:
                    if not panel.login_manager:
                        raise ProviderError("unsupported", "本机模式请在独立窗口登录后使用只读验证")
                    operation = payload.get("operation_id")
                    action = panel.login_manager.complete if self.path.endswith("/complete") else panel.login_manager.cancel
                    result = action(key, operation)
                elif self.path == "/api/probe":
                    result = panel.probe(key)
                elif self.path == "/api/config/read":
                    result = panel.store.read()["accounts"].get(key, {})
                elif self.path == "/api/config/save":
                    if panel.authorization.read(key)["status"] == "authorizing":
                        raise ProviderError("authorization_in_progress", "正在授权，请先完成或取消再修改配置")
                    panel.store.update(key, payload["settings"])
                    result = {"status": "saved"}
                elif self.path == "/api/notifications/save":
                    result = {"status": "saved", "rule": panel.notifications.update(key, payload["rule"])}
                else:
                    return self.respond({"error": "not found"}, 404)
                self.respond(result)
            except ProviderError as exc:
                self.respond({"error": str(exc)}, 400)
            except (ValueError, KeyError, TypeError):
                self.respond({"error": "请求格式错误"}, 400)
            except Exception:
                self.respond({"error": "操作失败，请检查本地环境"}, 500)
    return Handler


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=18761)
    args = p.parse_args()
    try:
        configured_origins()
    except ValueError as exc:
        p.error(str(exc))
    panel = Panel()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(panel))
    print(f"账号接入面板：http://127.0.0.1:{server.server_port} （仅本机可访问）", flush=True)
    try:
        server.serve_forever()
    finally:
        panel.close()
        server.server_close()


if __name__ == "__main__":
    main()
