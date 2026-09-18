"""Validated local account settings with serialized, atomic updates."""
import copy
import json
import os
import fcntl
from runtime import PROVIDER_CONFIG
from .base import ProviderError
from .credentials import private_json
from .identity import validate_aliases


def validate_settings(settings):
    if not isinstance(settings, dict):
        raise ProviderError("invalid_config", "账号配置必须是对象")
    forbidden = {"password", "cookie", "access_token", "app_secret", "secret", "authorization"}

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in forbidden:
                    raise ProviderError("invalid_config", "配置中仅保存凭证环境变量名，不保存明文凭证")
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(settings)
    validate_aliases(settings.get("content_aliases", {}))
    for aliases in settings.get("comment_aliases", {}).values():
        validate_aliases(aliases)
    for recipe in settings.get("workflows", {}).values():
        if not isinstance(recipe, dict) or not recipe.get("url"):
            raise ProviderError("invalid_config", "每个采集流程需要入口 URL")
    return copy.deepcopy(settings)


class SettingsStore:
    def __init__(self, path=None):
        self.path = path or PROVIDER_CONFIG

    def read(self):
        try:
            value = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"schema_version": 1, "accounts": {}}
        except (ValueError, OSError):
            raise ProviderError("invalid_config", "账号配置文件损坏，已停止覆盖") from None
        if not isinstance(value, dict) or not isinstance(value.get("accounts"), dict):
            raise ProviderError("invalid_config", "账号配置缺少 accounts 对象")
        return value

    def update(self, key, settings):
        settings = validate_settings(settings)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.with_suffix(".lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = self.read()
            value["accounts"][key] = settings
            private_json(self.path, value)
        return settings

    def compare_update(self, key, expected, settings):
        """Commit one account without overwriting concurrent operator changes."""
        settings = validate_settings(settings) if settings is not None else None
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.with_suffix(".lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = self.read()
            if value["accounts"].get(key) != expected:
                raise ProviderError("config_changed", "账号配置已被修改，请取消后重新开始授权")
            if settings is None:
                value["accounts"].pop(key, None)
            else:
                value["accounts"][key] = settings
            private_json(self.path, value)
        return settings
