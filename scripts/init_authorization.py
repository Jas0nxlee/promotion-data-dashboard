#!/usr/bin/env python3
"""Initialize private login credentials and the directory-mounted provider config.

Default mode generates a strong password and displays it once on an interactive
terminal. Noninteractive callers must supply --password-stdin; never argv/env.
Existing credentials and provider files are never replaced implicitly.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def password_hash(username, password):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}", username):
        raise ValueError("用户名仅允许 1–64 位字母、数字、点、下划线、@ 或连字符")
    if len(password) < 16 or len(password.encode("utf-8")) > 72 or any(c in password for c in "\r\n\0"):
        raise ValueError("口令至少 16 个字符，UTF-8 不超过 72 字节，且不能含换行")
    executable = shutil.which("htpasswd")
    if not executable:
        raise RuntimeError("缺少 htpasswd；请安装 apache2-utils，或在 login 镜像中运行初始化工具")
    result = subprocess.run([executable, "-n", "-B", "-C", "12", "-i", username],
                            input=password + "\n", text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode or not re.fullmatch(re.escape(username) + r":\$2[aby]\$12\$[./A-Za-z0-9]{53}\s*", result.stdout):
        raise RuntimeError("htpasswd 未能生成 bcrypt 凭证；未写入文件")
    return result.stdout.strip() + "\n"


def atomic_private(path, value, *, replace=False):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("拒绝覆盖符号链接")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)  # Atomic create-only, including against symlink races.
    finally:
        Path(temporary).unlink(missing_ok=True)


def initialize(auth_dir, provider_dir, sessions_dir, username, password, *, migrate=None, replace_password=False):
    auth = Path(auth_dir) / "admin.htpasswd"
    provider = Path(provider_dir) / "providers.json"
    if (auth.exists() or auth.is_symlink()) and not replace_password:
        raise FileExistsError("管理员凭证已存在；仅显式 --replace-password 才能重置")
    source = None
    if migrate:
        if provider.exists() or provider.is_symlink():
            raise FileExistsError("目标 providers.json 已存在，拒绝覆盖或重复迁移")
        source = Path(migrate).read_text(encoding="utf-8")
        value = json.loads(source)
        if not isinstance(value, dict) or not isinstance(value.get("accounts"), dict):
            raise ValueError("旧 provider 文件必须含 accounts 对象")
        for settings in value["accounts"].values():
            if not isinstance(settings, dict):
                raise ValueError("旧 provider 文件中的每个账号配置必须为对象")
            if settings.get("provider", "browser") != "wechat_official":
                settings.update(session_mode="portable", channel="chromium")
                settings.pop("cdp_url", None)
                settings.pop("headed", None)
        source = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    elif not provider.exists():
        source = json.dumps({"schema_version": 1, "accounts": {}}, indent=2) + "\n"
    if provider.is_symlink():
        raise ValueError("拒绝使用 provider 配置符号链接")
    hashed = password_hash(username, password)
    for folder in (Path(auth_dir), Path(provider_dir), Path(sessions_dir), Path(sessions_dir) / "authorization"):
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if source is not None:
        atomic_private(provider, source)
    atomic_private(auth, hashed, replace=replace_password)
    return auth, provider


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-dir", type=Path, default=ROOT / ".runtime/authorization")
    parser.add_argument("--provider-dir", type=Path, default=ROOT / ".runtime/provider-config")
    parser.add_argument("--sessions-dir", type=Path, default=ROOT / ".runtime/sessions")
    parser.add_argument("--username", default="operator")
    parser.add_argument("--migrate-provider-file", type=Path)
    parser.add_argument("--replace-password", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--password-stdin", action="store_true")
    mode.add_argument("--prompt-password", action="store_true")
    args = parser.parse_args(argv)
    legacy = ROOT / ".runtime/providers.json"
    if (not args.migrate_provider_file and not (args.provider_dir / "providers.json").exists()
            and args.provider_dir.resolve() == (ROOT / ".runtime/provider-config").resolve() and legacy.exists()):
        parser.error("发现旧 .runtime/providers.json；请显式传 --migrate-provider-file .runtime/providers.json，原文件不会更改")
    generated = False
    if args.password_stdin:
        password = sys.stdin.read().rstrip("\r\n")
    else:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            parser.error("自动生成或交互输入口令只能在真实终端运行；自动化请使用 --password-stdin")
        if args.prompt_password:
            password = getpass.getpass("管理员口令（至少16字符）: ")
            if password != getpass.getpass("再次输入: "):
                parser.error("两次输入不一致")
        else:
            password, generated = secrets.token_urlsafe(24), True
    try:
        auth, provider = initialize(args.auth_dir, args.provider_dir, args.sessions_dir,
                                    args.username, password, migrate=args.migrate_provider_file,
                                    replace_password=args.replace_password)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(f"管理员: {args.username}\n凭证哈希: {auth.resolve()}\n账号配置: {provider.resolve()}")
    if generated:
        print("初始口令（仅本次终端显示，请保存）: " + password)


if __name__ == "__main__":
    main()
