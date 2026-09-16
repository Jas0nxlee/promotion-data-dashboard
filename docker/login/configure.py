"""Validate the shared bcrypt credential and render nginx without shell interpolation."""
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


def validate_origin(origin):
    parsed = urlsplit(origin)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("PROMOTION_PANEL_ORIGIN 端口无效") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
            or not re.fullmatch(r"[A-Za-z0-9.\[\]:-]+", parsed.netloc)
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("PROMOTION_PANEL_ORIGIN 必须是完整的同源 HTTP(S) 地址，不含路径或凭证")
    return parsed.netloc


def render(template, origin, auth_file):
    host = validate_origin(origin)
    path = Path(auth_file)
    if not path.is_absolute() or not re.fullmatch(r"[A-Za-z0-9_./-]+", str(path)):
        raise ValueError("PROMOTION_AUTH_FILE 必须是容器内绝对路径")
    if not path.is_file() or path.is_symlink():
        raise ValueError("缺少管理员凭证文件；请先运行 init_authorization.py")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if len(lines) != 1 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}:\$2[aby]\$(?:1[0-6])\$[./A-Za-z0-9]{53}", lines[0]):
        raise ValueError("管理员凭证必须是单个有效的 bcrypt htpasswd 条目，成本至少为 10")
    if path.stat().st_mode & 0o077:
        raise ValueError("管理员凭证文件权限必须为 0600")
    return template.replace("@@PANEL_ORIGIN@@", origin).replace("@@PANEL_HOST@@", host).replace("@@AUTH_FILE@@", str(path))


def main():
    folder = Path(__file__).resolve().parent
    target = Path("/tmp/promotion-login-nginx.conf")
    value = render((folder / "nginx.conf.template").read_text(),
                   os.environ.get("PROMOTION_PANEL_ORIGIN", "http://127.0.0.1:18762"),
                   os.environ.get("PROMOTION_AUTH_FILE", "/run/promotion/authorization/admin.htpasswd"))
    target.write_text(value)
    target.chmod(0o600)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
