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
from providers.health import read_verification, record_verification, record_comment_verification

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
        missing = [] if config.get("provider") in {"bilibili_creator", "douyin_creator", "wechat_channels_creator", "xiaohongshu_creator", "zhihu_creator", "wechat_official"} else [x for x in required if x not in config.get("workflows", {})]
        result.append({"account": key, "configured": bool(config),
                       "missing_workflows": missing, "session_saved": (SESSIONS / session_key(key)).is_dir(),
                       "comment_identity_verified": config.get("comment_identity_compatible") is True,
                       "live_verification": read_verification(key, config)})
    print(json.dumps(result, ensure_ascii=False, indent=2))


def login(account, channel):
    from playwright.sync_api import sync_playwright
    key = f"{account['platform']}:{account['account_name']}"
    with account_lock(key), sync_playwright() as p:
        folder = SESSIONS / session_key(key)
        folder.mkdir(mode=0o700, exist_ok=True)
        context = p.chromium.launch_persistent_context(str(folder), channel=channel, headless=False,
                    ignore_default_args=["--password-store=basic", "--use-mock-keychain"])
        try:
            page = context.new_page()
            page.goto(ENTRIES[account["platform"]], wait_until="domcontentloaded")
            input("请在独立浏览器完成登录；确认账号后按回车保存会话（不会自动认定身份已验证）：")
        finally:
            context.close()
        os.chmod(folder, 0o700)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("status", "login", "probe", "probe-bilibili", "probe-comments", "reconcile", "export-session", "bind-channels"))
    p.add_argument("--account", help="platform:account_name")
    p.add_argument("--channel", default="chrome")
    p.add_argument("--bvid")
    p.add_argument("--content-id")
    p.add_argument("--previous", type=Path)
    p.add_argument("--incoming", type=Path)
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
    if args.command == "bind-channels":
        if account["platform"] != "wechat_channels":
            p.error("此绑定命令仅用于视频号")
        from providers.settings import SettingsStore
        from providers.wechat_channels import WeChatChannelsProvider
        store = SettingsStore()
        settings = store.read()["accounts"].get(args.account, {"channel": args.channel})
        provider = WeChatChannelsProvider(account, settings)
        provider.browser.settings["session_mode"] = "interactive"
        settings = provider.bind_from_login()
        store.update(args.account, settings)
        print("已按后台稳定短号绑定视频号身份；尚需验证作品和评论采集")
        return
    if args.command == "export-session":
        provider = ProviderRegistry().get(account)
        if not hasattr(provider, "browser"):
            p.error("官方接口提供器不使用浏览器会话")
        provider.browser.settings["session_mode"] = "interactive"
        with provider.browser.session():
            provider._profile()
            path = provider.browser.export_session()
        print(f"已导出此账号平台会话：{path.name}；文件权限 600，不包含其他平台登录态")
        return
    if args.command == "reconcile":
        from providers.identity import reconcile_contents
        if not args.previous or not args.incoming:
            p.error("reconcile 需要 --previous 和 --incoming JSON 文件")
        before = json.loads(args.previous.read_text())
        after = json.loads(args.incoming.read_text())
        video = account["platform"] in {"bilibili", "douyin", "wechat_channels"}
        key = "videos" if video else "articles"
        previous = [r for r in before.get(key, []) if r.get("account_key") == args.account]
        incoming = after.get("records", after.get(key, []))
        result = reconcile_contents(previous, incoming, "video_id" if video else "article_id")
    elif args.command == "probe-bilibili":
        if account["platform"] != "bilibili" or not args.bvid:
            p.error("公开详情验证需要 B站账号和 --bvid")
        from providers.bilibili import BilibiliProvider
        result = BilibiliProvider(account, {}).video_detail(args.bvid)
        # Validate public content ownership against the configured account.
        if result["source_author_id"] != str(account["platform_uid"]):
            raise ProviderError("identity_mismatch", "视频原作者与选定账号不一致")
    else:
        from dataclasses import asdict
        registry = ProviderRegistry()
        settings = registry.config.get("accounts", {}).get(args.account, {})
        try:
            provider = registry.get(account)
            if args.command == "probe-comments":
                if not args.content_id:
                    p.error("评论验证需要 --content-id")
                comments, stats = provider.comments({**account, "content_id": args.content_id}, args.max_pages, True)
                result = {"comments": comments, "stats": stats}
                record_comment_verification(args.account, settings, comments, stats=stats)
            else:
                collection = provider.collect(max_pages=args.max_pages)
                record_verification(args.account, settings, collection)
                result = asdict(collection)
        except ProviderError as error:
            record_verification(args.account, settings, error=error)
            raise
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
