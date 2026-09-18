#!/usr/bin/env python3
"""Build and verify only a uniquely named, disposable login deployment."""
import argparse
import base64
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time

from init_authorization import initialize, atomic_private

ROOT = Path(__file__).resolve().parents[1]


def smoke(project, *, skip_build=False):
    if not re.fullmatch(r"promotion-login-smoke-[a-z0-9][a-z0-9-]{0,40}", project):
        raise ValueError("测试项目名必须是唯一的 promotion-login-smoke-*，拒绝操作其他项目")
    existing = subprocess.check_output(["docker", "ps", "-a", "-q", "--filter",
                                        "label=com.docker.compose.project=" + project], text=True)
    if existing.strip():
        raise ValueError("这个测试项目已有容器，请改用新的唯一项目名")
    folder = ROOT / ".runtime" / project
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    auth, providers, sessions = (folder / name for name in ("auth", "providers", "sessions"))
    runtime, data = folder / "login-runtime", folder / "data"
    runtime.mkdir(); data.mkdir()
    username, password = "smoke-operator", secrets.token_urlsafe(24)
    initialize(auth, providers, sessions, username, password)
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        port = connection.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    mounts = [{"type": "bind", "source": str(source), "target": target}
              for source, target in ((data, "/app/data"), (sessions, "/app/data/sessions"),
                                     (runtime, "/app/.runtime"), (providers, "/run/promotion/providers"))]
    mounts.append({"type": "bind", "source": str(auth), "target": "/run/promotion/authorization", "read_only": True})
    overlay = folder / "compose.smoke.json"
    overlay.write_text(json.dumps({"services": {
        "collector": {"image": project + "-collector:smoke"},
        "login": {"image": project + "-login:smoke", "volumes": mounts},
    }}, indent=2))
    env = {**os.environ, "PROMOTION_DEV_ID": project, "PROMOTION_LOGIN_PORT": str(port),
           "PROMOTION_PANEL_ORIGIN": origin, "PROMOTION_PROVIDER_DIR": str(providers),
           "PROMOTION_SESSIONS_DIR": str(sessions), "PROMOTION_AUTH_DIR": str(auth),
           "PROMOTION_LOGIN_RUNTIME_DIR": str(runtime)}
    command = ["docker", "compose", "--env-file", os.devnull, "-p", project,
               "-f", str(ROOT / "docker-compose.local.yml"), "-f", str(overlay), "--profile", "login"]
    report = {"project": project, "gateway": origin, "checks": {}, "artifacts": str(folder)}
    started = False

    def run(arguments, *, check=True, filename=None):
        if filename:
            with (folder / filename).open("w") as output:
                result = subprocess.run(command + arguments, env=env, stdout=output, stderr=subprocess.STDOUT)
        else:
            result = subprocess.run(command + arguments, env=env, capture_output=True, text=True)
        if check and result.returncode:
            raise RuntimeError("隔离 Compose 操作失败；请检查本次测试目录中的日志")
        return result

    def request(path, *, authenticated=False, request_origin=None, websocket=False, wrong_password=False):
        headers = {}
        if authenticated:
            value = username + ":" + ("incorrect-test-password" if wrong_password else password)
            headers["Authorization"] = "Basic " + base64.b64encode(value.encode()).decode()
        if request_origin is not None:
            headers["Origin"] = request_origin
        if websocket:
            headers.update({"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
                            "Sec-WebSocket-Key": base64.b64encode(secrets.token_bytes(16)).decode(),
                            "Sec-WebSocket-Protocol": "binary"})
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            if response.status == 101:
                # websockify's binary protocol forwards the VNC server greeting.
                frame = response.fp.read(2)
                if len(frame) != 2 or frame[0] & 15 != 2 or frame[1] & 128 or frame[1] > 125:
                    raise RuntimeError("授权桌面未返回预期的 VNC WebSocket 数据")
                body = response.fp.read(frame[1])
            else:
                body = response.read()
            return response.status, body
        finally:
            connection.close()

    def expect(name, actual, wanted):
        report["checks"][name] = actual == wanted
        if actual != wanted:
            raise RuntimeError(f"隔离验证失败：{name}（实际 {actual}，预期 {wanted}）")

    try:
        if not skip_build:
            run(["build", "login"], filename="build.log")
        started = True
        run(["up", "-d", "--no-deps", "--no-build", "login"], filename="startup.log")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if request("/")[0] == 401 and request("/api/accounts", authenticated=True)[0] == 200:
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.5)
        else:
            raise RuntimeError("隔离登录容器 90 秒内未就绪")
        for path in ("/", "/api/accounts", "/desktop/", "/desktop/vnc.html", "/desktop/core/rfb.js"):
            expect("anonymous " + path, request(path)[0], 401)
            expect("authenticated " + path, request(path, authenticated=True)[0], 200)
        expect("wrong password rejected", request("/", authenticated=True, wrong_password=True)[0], 401)
        expect("websocket unauthenticated", request("/desktop/websockify", request_origin=origin, websocket=True)[0], 401)
        expect("websocket foreign Origin", request("/desktop/websockify", authenticated=True, request_origin="https://example.invalid", websocket=True)[0], 403)
        expect("websocket missing Origin", request("/desktop/websockify", authenticated=True, websocket=True)[0], 403)
        websocket_status, greeting = request("/desktop/websockify", authenticated=True, request_origin=origin, websocket=True)
        expect("websocket same Origin", websocket_status, 101)
        expect("VNC greeting reaches authenticated websocket", greeting.startswith(b"RFB "), True)
        expect("internal services healthy", run(["exec", "-T", "login", "python", "/app/docker/login/healthcheck.py"]).returncode, 0)

        status, body = request("/api/accounts", authenticated=True)
        accounts = json.loads(body)
        if isinstance(accounts, dict):
            accounts = accounts["accounts"]
        key = next(item["key"] for item in accounts if item["key"].startswith("bilibili:") and not item["configured"])
        second_key = next(item["key"] for item in accounts if item["key"].startswith("bilibili:") and not item["configured"] and item["key"] != key)
        reader_id = run(["ps", "-q", "login"]).stdout.strip()
        reader_before = json.loads(subprocess.check_output(["docker", "inspect", reader_id], env=env))[0]["State"]["StartedAt"]
        config_path = providers / "providers.json"
        visibility_started = time.monotonic()
        atomic_private(config_path, json.dumps({"accounts": {key: {"provider": "browser"}}}), replace=True)
        # macOS VM file-sharing may briefly cache directory entries. A file bind
        # pinned to the old inode would still fail this bounded visibility check.
        deadline, visible = time.monotonic() + 8, False
        while time.monotonic() < deadline:
            accounts = json.loads(request("/api/accounts", authenticated=True)[1])
            if isinstance(accounts, dict):
                accounts = accounts["accounts"]
            visible = next(item["configured"] for item in accounts if item["key"] == key)
            if visible:
                break
            time.sleep(.25)
        expect("atomic provider replacement visible", visible, True)
        report["host_replace_visibility_seconds"] = round(time.monotonic() - visibility_started, 3)
        report["host_replace_timeout_seconds"] = 8

        # Exercise the real writer used by authorization: its os.replace runs
        # inside the container, while the existing panel remains alive.
        code = ("import sys;sys.path.insert(0,'/app/pipeline');from providers.settings import SettingsStore;"
                "SettingsStore().update(" + repr(second_key) + ", {'provider':'browser'})")
        visibility_started = time.monotonic()
        run(["exec", "-T", "login", "python", "-c", code])
        accounts = json.loads(request("/api/accounts", authenticated=True)[1])
        if isinstance(accounts, dict):
            accounts = accounts["accounts"]
        expect("container atomic replacement visible", next(item["configured"] for item in accounts if item["key"] == second_key), True)
        expect("container replacement preserves other account", next(item["configured"] for item in accounts if item["key"] == key), True)
        report["container_replace_visibility_seconds"] = round(time.monotonic() - visibility_started, 3)

        ids = run(["ps", "-q"]).stdout.strip().splitlines()
        expect("only login service running", len(ids), 1)
        inspection = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], env=env))[0]
        expect("same running reader throughout replacements", (ids[0], inspection["State"]["StartedAt"]), (reader_id, reader_before))
        published = inspection["HostConfig"]["PortBindings"]
        expect("only gateway port published", sorted(published), ["18762/tcp"])
        expect("gateway loopback bind", published["18762/tcp"][0]["HostIp"], "127.0.0.1")
        expect("login service label", inspection["Config"]["Labels"]["com.docker.compose.service"], "login")
        missing = run(["run", "--rm", "--no-deps", "-e", "PROMOTION_AUTH_FILE=/missing/admin.htpasswd", "login"], check=False)
        expect("missing credential refuses startup", missing.returncode != 0, True)
        report["success"] = True
    except Exception as error:
        report.update(success=False, error=str(error))
        raise
    finally:
        if started:
            run(["logs", "--no-color", "login"], check=False, filename="container.log")
            run(["down", "--remove-orphans"], check=False, filename="cleanup.log")
        (folder / "smoke-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    try:
        smoke(args.project, skip_build=args.skip_build)
    except (OSError, ValueError, RuntimeError, StopIteration):
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
