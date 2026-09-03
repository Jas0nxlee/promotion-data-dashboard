#!/usr/bin/env python3
"""发送 comment_monitor.py 生成的 SMTP 邮件队列。"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

from snapshot_utils import atomic_write_json


ROOT = Path(__file__).resolve().parent.parent
ALERT_PATH = ROOT / "data" / "comment_alert.json"


def load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def as_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def smtp_config() -> tuple[dict, list[str]]:
    use_ssl = as_bool("SMTP_SSL", False)
    config = {
        "host": os.environ.get("SMTP_HOST", "").strip(),
        "port": int(os.environ.get("SMTP_PORT", "465" if use_ssl else "587")),
        "username": os.environ.get("SMTP_USERNAME", "").strip(),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "from_addr": os.environ.get("SMTP_FROM", "").strip(),
        "use_ssl": use_ssl,
        "starttls": as_bool("SMTP_STARTTLS", not use_ssl),
        "timeout": float(os.environ.get("SMTP_TIMEOUT", "30")),
    }
    missing = [key for key in ("host", "from_addr") if not config[key]]
    if bool(config["username"]) != bool(config["password"]):
        missing.append("SMTP_USERNAME/SMTP_PASSWORD 必须同时配置")
    return config, missing


def load_queue(path: Path) -> dict:
    if not path.exists():
        return {"emails": [], "unmapped": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"邮件队列无法读取: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("emails", []), list):
        raise RuntimeError("邮件队列格式错误: emails 必须是数组")
    return payload


def save_queue(path: Path, payload: dict) -> None:
    # 只删除已全部发完且没有人工待处理项的空队列。
    if not payload.get("emails") and not payload.get("unmapped"):
        path.unlink(missing_ok=True)
        return
    for key in ("to", "subject", "body", "body_format"):
        payload.pop(key, None)
    if len(payload.get("emails", [])) == 1:
        email = payload["emails"][0]
        for key in ("to", "subject", "body", "body_format"):
            if key in email:
                payload[key] = email[key]
    atomic_write_json(path, payload, pretty=True)


def connect(config: dict):
    context = ssl.create_default_context()
    if config["use_ssl"]:
        client = smtplib.SMTP_SSL(
            config["host"], config["port"], timeout=config["timeout"], context=context)
    else:
        client = smtplib.SMTP(config["host"], config["port"], timeout=config["timeout"])
        client.ehlo()
        if config["starttls"]:
            client.starttls(context=context)
            client.ehlo()
    if config["username"]:
        client.login(config["username"], config["password"])
    return client


def make_message(item: dict, from_addr: str) -> EmailMessage:
    for key in ("to", "subject", "body"):
        if not str(item.get(key) or "").strip():
            raise ValueError(f"待发邮件缺少字段: {key}")
    message = EmailMessage()
    message["From"] = from_addr
    message["To"] = item["to"]
    message["Subject"] = item["subject"]
    message.set_content(item["body"])
    return message


def send_pending(path: Path, config: dict) -> int:
    payload = load_queue(path)
    pending = payload.get("emails", [])
    if not pending:
        print("没有待发送邮件。")
        return 0

    sent = 0
    with connect(config) as client:
        while payload.get("emails"):
            item = payload["emails"][0]
            message = make_message(item, config["from_addr"])
            client.send_message(message)
            payload["emails"].pop(0)
            save_queue(path, payload)
            sent += 1
            print(f"已发送：{item.get('to')} · {item.get('subject')}")
    print(f"发送完成：{sent} 封。")
    return sent


def main() -> None:
    parser = argparse.ArgumentParser(description="发送评论提醒 SMTP 队列")
    parser.add_argument("--check-config", action="store_true", help="只检查 SMTP 配置，不联网")
    parser.add_argument("--queue", default=str(ALERT_PATH), help="待发邮件 JSON 路径")
    args = parser.parse_args()

    load_dotenv()
    config, problems = smtp_config()
    if problems:
        print("SMTP 配置未就绪：" + "、".join(problems), file=sys.stderr)
        raise SystemExit(2)
    if args.check_config:
        mode = "SSL" if config["use_ssl"] else "STARTTLS" if config["starttls"] else "明文"
        auth = "已配置认证" if config["username"] else "无认证"
        print(f"SMTP 配置完整：{config['host']}:{config['port']} · {mode} · {auth}")
        return
    send_pending(Path(args.queue), config)


if __name__ == "__main__":
    main()
