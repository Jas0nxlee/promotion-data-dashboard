#!/usr/bin/env python3
"""Local account session setup and read-only diagnostics, never sends mail."""
import argparse
import json
import os
from pathlib import Path
from runtime import ROOT, SESSIONS, PROVIDER_CONFIG
from providers.base import ProviderError
from providers.browser import session_key, account_lock
from providers.registry import PLATFORMS, ProviderRegistry

ENTRIES = {
    "bilibili": "https://member.bilibili.com/platform/home",
    "douyin": "https://creator.douyin.com/",
    "xiaohongshu": "https://creator.xiaohongshu.com/",
    "zhihu": "https://www.zhihu.com/creator",
    "wechat_channels": "https://channels.weixin.qq.com/",
    "wechat_service": "https://mp.weixin.qq.com/",
    "wechat_subscription": "https://mp.weixin.qq.com/",
}


def accounts():
    result = {}
    for name in ("accounts.json", "article_accounts.json"):
        for a in json.loads((ROOT / "config" / name).read_text())["accounts"]:
            if a["platform"] in PLATFORMS:
                result[f"{a['platform']}:{a['account_name']}"] = a
    return result


def status():
    registry = ProviderRegistry()
    result = []
    for key, a in accounts().items():
        config = registry.config.get("accounts", {}).get(key, {})
        required = ["profile", "contents"]
        if a["platform"] in {"bilibili", "douyin", "xiaohongshu", "wechat_channels"}:
            required.extend(["comments", "replies"])
        missing = [x for x in required if x not in config.get("workflows", {})]
        result.append({"account": key, "configured": bool(config),
                       "missing_workflows": missing, "session_saved": (SESSIONS / session_key(key)).is_dir(),
                       "comment_identity_verified": config.get("comment_identity_compatible") is True,
                       "live_verified": False})
    print(json.dumps(result, ensure_ascii=False, indent=2))


def login(account, channel):
    from playwright.sync_api import sync_playwright
    key = f"{account['platform']}:{account['account_name']}"
    with account_lock(key), sync_playwright() as p:
        folder = SESSIONS / session_key(key)
        folder.mkdir(mode=0o700, exist_ok=True)
        context = p.chromium.launch_persistent_context(str(folder), channel=channel, headless=False)
        try:
            page = context.new_page()
            page.goto(ENTRIES[account["platform"]], wait_until="domcontentloaded")
            input("请在独立浏览器完成登录；确认账号后按回车保存会话（不会自动认定身份已验证）：")
        finally:
            context.close()
        os.chmod(folder, 0o700)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("status", "login", "probe", "probe-bilibili"))
    p.add_argument("--account", help="platform:account_name")
    p.add_argument("--channel", default="chrome")
    p.add_argument("--bvid")
    p.add_argument("--max-pages", type=int, default=2)
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    if args.command == "status":
        status()
        return
    account = accounts().get(args.account)
    if not account:
        p.error("请从 status 输出中选择已配置的 --account")
    if args.command == "login":
        login(account, args.channel)
        return
    if args.command == "probe-bilibili":
        if account["platform"] != "bilibili" or not args.bvid:
            p.error("公开详情验证需要 B站账号和 --bvid")
        from providers.bilibili import BilibiliProvider
        result = BilibiliProvider(account, {}).video_detail(args.bvid)
        # Validate public content ownership against the configured account.
        if result["source_author_id"] != str(account["platform_uid"]):
            raise ProviderError("identity_mismatch", "视频原作者与选定账号不一致")
    else:
        from dataclasses import asdict
        result = asdict(ProviderRegistry().get(account).collect(max_pages=args.max_pages))
    if args.output:
        dest = args.output.resolve()
        # Diagnostic output is always separate from dashboard and mail state.
        private = (ROOT / ".runtime").resolve()
        if private not in dest.parents:
            p.error("只读验证输出必须放在当前工作树 .runtime 目录")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        os.chmod(dest, 0o600)
        print(f"验证结果已写入 {dest.name}，未更新大屏与提醒状态")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except ProviderError as exc:
        print(str(exc))
        raise SystemExit(2)
