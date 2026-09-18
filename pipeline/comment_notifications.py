"""Account-specific comment mail preferences shared by login and collector containers."""

import fcntl
import json
import os
import re
from pathlib import Path

from runtime import DATA
from providers.base import ProviderError
from providers.credentials import private_json


SETTINGS_PATH = DATA / "comment_notification_settings.json"
EMAIL_PATTERN = re.compile(r"^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$")


def validate_rule(rule):
    if not isinstance(rule, dict) or rule.get("mode") not in {"inherit", "enabled", "disabled"}:
        raise ProviderError("invalid_notification", "请选择平台默认、开启或关闭")
    mode = rule["mode"]
    if mode != "enabled":
        return {"mode": mode}
    email = rule.get("email")
    owner = rule.get("owner", "")
    if not isinstance(email, str) or len(email) > 254 or not EMAIL_PATTERN.fullmatch(email.strip()):
        raise ProviderError("invalid_notification", "请填写有效的收件人邮箱")
    if not isinstance(owner, str) or len(owner) > 80 or any(ord(ch) < 32 for ch in owner):
        raise ProviderError("invalid_notification", "负责人名称格式无效")
    return {"mode": mode, "email": email.strip(), "owner": owner.strip()}


class NotificationStore:
    def __init__(self, path=None):
        self.path = Path(path or SETTINGS_PATH)

    def read(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise ProviderError("invalid_notification", "邮件通知配置无法读取，已停止发送") from None
        if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("accounts"), dict):
            raise ProviderError("invalid_notification", "邮件通知配置格式错误，已停止发送")
        return {key: validate_rule(rule) for key, rule in value["accounts"].items()}

    def update(self, key, rule):
        rule = validate_rule(rule)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.with_suffix(".lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            rules = self.read()
            if rule["mode"] == "inherit":
                rules.pop(key, None)
            else:
                rules[key] = rule
            private_json(self.path, {"schema_version": 1, "accounts": rules})
        return rule


def effective_rule(key, platform, rules, platform_recipients):
    rule = rules.get(key, {"mode": "inherit"})
    mode = rule["mode"]
    if mode == "disabled":
        return {"mode": mode, "enabled": False, "email": "", "owner": ""}
    source = rule if mode == "enabled" else platform_recipients.get(platform, {})
    return {"mode": mode, "enabled": bool(source.get("email")),
            "email": source.get("email", ""), "owner": source.get("owner", "")}
