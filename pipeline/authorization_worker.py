"""Validate an isolated login candidate; never publish snapshots or send alerts."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
from api_budget import ApiBudgetExceeded

from provider_setup import accounts
from providers.base import ProviderError
from providers.credentials import private_json
from providers.registry import ProviderRegistry
from providers.settings import SettingsStore
from providers.health import record_verification


def expected_identity(account, settings):
    value = (account.get('provided_id') or account.get('platform_uid')) if settings.get('provider') == 'xiaohongshu_creator' else account.get('platform_uid')
    return str(value or '')


def validate_candidate(key, max_pages=200):
    account = accounts().get(key)
    if not account:
        raise ProviderError("unknown_account", "账号不在授权平台清单中")
    store = SettingsStore()
    settings = store.read()["accounts"][key]
    provider = ProviderRegistry().get(account)
    if not hasattr(provider, "browser") or not hasattr(provider, "_profile"):
        raise ProviderError("unsupported", "该数据源不支持浏览器授权，请使用其专用配置")
    provider.browser.settings["session_mode"] = "interactive"
    if settings.get("provider") == "wechat_channels_creator" and not settings.get("expected_finder_id"):
        settings = provider.bind_from_login()
        store.update(key, settings)
        provider = ProviderRegistry().get(account)
        provider.browser.settings["session_mode"] = "interactive"
    with provider.browser.session():
        identity = provider._profile()
        if not isinstance(identity, dict) or identity.get('verified_account_id') != expected_identity(account, settings):
            raise ProviderError('identity_mismatch', '实际登录账号与项目账号清单不一致，未导出候选会话')
        # The candidate session directory is private to this operation.
        provider.browser.export_session()
    settings = {**settings, "session_mode": "portable"}
    settings.pop("cdp_url", None)
    settings.pop("headed", None)
    store.update(key, settings)
    provider = ProviderRegistry().get(account)
    result = provider.collect(max_pages=max_pages)
    if not result.profile.get("verified_account_id"):
        raise ProviderError("verification_incomplete", "会话已读取，但账号身份尚未通过验证")
    if not result.complete:
        raise ProviderError("verification_incomplete", "作品目录尚未完整：" + result.note)
    if result.profile['verified_account_id'] != expected_identity(account, settings):
        raise ProviderError('identity_mismatch', '会话恢复后的账号与项目账号清单不一致')
    # Keep candidate health separate; do not advertise authorization before commit.
    from runtime import PROVIDER_CONFIG
    evidence = record_verification(key, settings, result, directory=PROVIDER_CONFIG.parent / "verification")
    if not evidence["metrics_verified"]:
        raise ProviderError("verification_incomplete", "必要指标覆盖不足，尚未保存为正式授权")
    comments, stats = None, None
    if account["platform"] in {"bilibili", "douyin", "wechat_channels", "xiaohongshu"} and result.records:
        samples = sorted(result.records, key=lambda r: (r.get("stats", {}).get("comment") or 0) > 0, reverse=True)
        sample = samples[0]
        cid = sample.get("video_id") or sample.get("article_id")
        comments, stats = provider.comments({**account, "content_id": cid}, max_pages=max_pages, include_replies=True)
        if stats.get('comments_complete') is not True or stats.get('replies_complete') is not True:
            raise ProviderError('verification_incomplete', '评论或回复读取尚未完整，未保存为正式授权')
        # Retain only the fields required to assess reply coverage, never text/authors.
        comments = [{"parent_comment_id": r.get("parent_comment_id", ""), "reply_count": r.get("reply_count")}
                    for r in comments]
        stats = {name: stats.get(name) for name in
                 ("comments_complete", "replies_complete", "expected_replies")}
    return {"success": True, "settings": settings, "collection": asdict(result),
            "comments": comments, "comment_stats": stats}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-pages", type=int, default=500)
    args = parser.parse_args()
    try:
        result = validate_candidate(args.account, args.max_pages)
    except ApiBudgetExceeded:
        result = {"success": False, "reason": "budget_exceeded",
                  "message": "今日采集请求额度已用完，请等待额度恢复或由管理员调整限额后重新验证；无需因此重新扫码"}
    except ProviderError as exc:
        result = {"success": False, "reason": exc.reason,
                  "message": str(exc).removeprefix(exc.reason + ": ")}
    except Exception:
        result = {"success": False, "reason": "verification_failed", "message": "验证未完成，请检查登录状态后重试"}
    private_json(args.output, result)
    raise SystemExit(0 if result["success"] else 2)


if __name__ == "__main__":
    main()
