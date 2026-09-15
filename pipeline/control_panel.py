#!/usr/bin/env python3
"""Loopback-only account onboarding panel. Never starts the production scheduler."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
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


class Panel:
    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.store = SettingsStore()
        self.jobs = {}
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.lock = threading.Lock()

    def summary(self):
        config = self.store.read()["accounts"]
        result = []
        for key, account in accounts().items():
            settings = config.get(key, {})
            result.append({"key": key, "name": account["account_name"], "platform": account["platform"],
                           "configured": bool(settings), "health": read_verification(key, settings),
                           "job": self.jobs.get(key, {})})
        return result

    def login(self, key):
        account = accounts()[key]
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
        defaults = {"bilibili": "bilibili_creator", "douyin": "douyin_creator", "wechat_channels": "wechat_channels_creator", "xiaohongshu": "xiaohongshu_creator", "zhihu": "zhihu_creator"}
        current.setdefault("provider", defaults.get(account["platform"], "browser"))
        if account["platform"] == "bilibili":
            current["expected_uid"] = str(account["platform_uid"])
        if account["platform"] == "wechat_channels":
            current.setdefault("expected_sph", str(account["platform_uid"]))
        current.update({"channel": "chrome", "cdp_url": f"http://127.0.0.1:{port}"})
        current.setdefault("comment_identity_compatible", False)
        self.store.update(key, current)
        return {"status": "login_opened", "message": "请在独立浏览器扫码；尚未认定登录或采集成功"}

    def probe(self, key):
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
                if key.startswith("wechat_channels:") and not settings.get("expected_finder_id"):
                    binding = subprocess.run([sys.executable, str(ROOT / "pipeline/provider_setup.py"), "bind-channels", "--account", key],
                                             env=env, capture_output=True, text=True, timeout=120)
                    if binding.returncode:
                        self.jobs[key] = {"running": False, "success": False, "message": "身份绑定未完成，请确认已登录且视频号短号与配置一致"}
                        return
                    settings = self.store.read()["accounts"].get(key, {})
                if settings.get("session_mode") == "portable" and settings.get("cdp_url"):
                    exported = subprocess.run([sys.executable, str(ROOT / "pipeline/provider_setup.py"), "export-session", "--account", key],
                                              env=env, capture_output=True, text=True, timeout=120)
                    if exported.returncode:
                        self.jobs[key] = {"running": False, "success": False, "message": "登录会话更新未完成，请确认独立浏览器中的账号身份"}
                        return
                result = subprocess.run([sys.executable, str(ROOT / "pipeline/provider_setup.py"), "probe", "--account", key,
                                         "--max-pages", "20", "--output", str(destination)], env=env,
                                        capture_output=True, text=True, timeout=180)
                message = "验证结果已保存；以覆盖检查为准" if result.returncode == 0 else "未完成，请检查登录状态与采集配置"
                self.jobs[key] = {"running": False, "success": result.returncode == 0, "message": message}
            except subprocess.TimeoutExpired:
                self.jobs[key] = {"running": False, "success": False, "message": "验证超时，未更新正式数据"}
        self.executor.submit(execute)
        return {"status": "running"}


def handler(panel):
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
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

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
            else:
                self.respond({"error": "not found"}, 404)

        def do_POST(self):
            origin = self.headers.get("Origin", "")
            if not self.host_allowed() or self.headers.get("X-CSRF-Token") != panel.token or origin not in {
                    f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                return self.respond({"error": "页面会话已更新或请求来源不符，请刷新本页面后重试"}, 403)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 200000:
                    return self.respond({"error": "invalid body size"}, 413)
                payload = json.loads(self.rfile.read(size))
                key = payload["account"]
                if key not in accounts():
                    raise ProviderError("unknown_account", "账号不在本项目清单内")
                if self.path == "/api/login":
                    result = panel.login(key)
                elif self.path == "/api/probe":
                    result = panel.probe(key)
                elif self.path == "/api/config/read":
                    result = panel.store.read()["accounts"].get(key, {})
                elif self.path == "/api/config/save":
                    panel.store.update(key, payload["settings"])
                    result = {"status": "saved"}
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
    panel = Panel()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(panel))
    print(f"账号接入面板：http://127.0.0.1:{server.server_port} （仅本机可访问）", flush=True)
    try:
        server.serve_forever()
    finally:
        panel.executor.shutdown(wait=False, cancel_futures=True)
        server.server_close()


if __name__ == "__main__":
    main()
