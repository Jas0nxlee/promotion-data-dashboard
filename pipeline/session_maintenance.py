#!/usr/bin/env python3
"""Periodically reuse verified video-channel sessions without changing snapshots or mail."""

import argparse
import time
from datetime import datetime, timezone

from runtime import DATA, SESSIONS
from provider_setup import accounts
from providers.authorization import AuthorizationStore
from providers.base import ProviderError
from providers.browser import session_key
from providers.credentials import private_json
from providers.registry import ProviderRegistry


STATE_PATH = DATA / "session_maintenance_state.json"


def maintain_once(*, idle_seconds=10800, clock=None, catalog=None, registry=None,
                  authorization=None, sessions=None, state_path=None):
    """Refresh only idle, authorized sessions; a failed identity check never exports."""
    clock = clock or time.time
    catalog = catalog if catalog is not None else accounts()
    registry = registry or ProviderRegistry()
    authorization = authorization or AuthorizationStore()
    sessions = sessions or SESSIONS
    state_path = state_path or STATE_PATH
    instant = clock()
    results = {}
    for key, account in catalog.items():
        if account.get("platform") != "wechat_channels":
            continue
        status = authorization.read(key).get("status")
        if status != "authorized":
            results[key] = {"status": "waiting_for_authorization"}
            continue
        portable = sessions / (session_key(key) + ".storage.json")
        try:
            age = instant - portable.stat().st_mtime
        except FileNotFoundError:
            age = idle_seconds
        if age < idle_seconds:
            results[key] = {"status": "recent_session"}
            continue
        try:
            provider = registry.get(account)
            if provider.settings.get("provider") != "wechat_channels_creator":
                raise ProviderError("setup_required", "视频号后台采集配置未就绪")
            with provider.browser.session():
                profile = provider._profile()
                if profile.get("verified_account_id") != account.get("platform_uid"):
                    raise ProviderError("identity_mismatch", "视频号续用检查的账号身份不一致")
            results[key] = {"status": "refreshed"}
        except ProviderError as exc:
            results[key] = {"status": "busy" if exc.reason == "session_busy" else "failed",
                            "reason": exc.reason}
        except Exception:
            results[key] = {"status": "failed", "reason": "maintenance_error"}
    report = {"checked_at": datetime.fromtimestamp(instant, timezone.utc).isoformat(),
              "idle_seconds": idle_seconds, "accounts": results}
    private_json(state_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="执行一次并退出")
    parser.add_argument("--idle-seconds", type=int, default=10800,
                        help="会话至少闲置多久后检查，默认 3 小时")
    parser.add_argument("--interval-seconds", type=int, default=3600,
                        help="后台循环间隔，默认 1 小时")
    args = parser.parse_args()
    if args.idle_seconds < 3600 or args.interval_seconds < 600:
        parser.error("续用检查间隔过短")
    while True:
        report = maintain_once(idle_seconds=args.idle_seconds)
        print(report, flush=True)
        if args.once:
            return
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
