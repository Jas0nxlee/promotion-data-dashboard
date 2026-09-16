#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
评论监控与邮件提醒
==================
定时检查所有平台内容的新评论，发现新评论时发送邮件提醒。

覆盖平台：
  - 可抓评论正文：抖音、B站、小红书、视频号（通过 平台评论数据源）
  - 仅评论数检测：CSDN、知乎、今日头条、搜狐、百家号、公众号等（无公开评论正文接口）

工作方式（尽量简单）：
  1. 从大屏数据 JSON（data/dashboard_data.json + data/article_dashboard_data.json）
     读取各账号的内容清单（作品/文章 + 平台 + 内容ID）。
  2. 按配置周期读取四个明细平台的最新一页作品。
  3. 按作品年龄分层检查一级评论；仅在 reply_count 增长时查询回复详情。
     只记录功能上线后的新评论与稳定用户 ID 匹配的官方回复。
  4. 仅计数的平台：对比每日大屏快照中的评论数字，增长即提醒。
  5. 有新增评论时，写入待发邮件队列，由 send_comment_alerts.py 发送。
  6. 所有平台请求遵循采集预算；时间线和 API 用量持久化到 data/。

用法:
    python pipeline/comment_monitor.py                # 全量检查一次
    python pipeline/comment_monitor.py --dry-run      # 只检查不发邮件
    python pipeline/comment_monitor.py --limit 5      # 可选：每账号只看最近 5 条内容
    python pipeline/comment_monitor.py --platform bilibili   # 只检查指定平台

依赖: requests
"""

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("缺少依赖: pip3 install requests")

from snapshot_utils import atomic_write_json
from api_budget import ApiBudget, ApiBudgetExceeded
import comment_timeline as timeline_store

from runtime import DATA, load_env as load_dotenv
from providers import ProviderRegistry
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = DATA
VIDEO_DATA = DATA_DIR / "dashboard_data.json"
ARTICLE_DATA = DATA_DIR / "article_dashboard_data.json"
STATE_PATH = DATA_DIR / "comment_state.json"
VIDEO_ACCOUNTS = ROOT / "config" / "accounts.json"
ARTICLE_ACCOUNTS = ROOT / "config" / "article_accounts.json"

CN_TZ = timezone(timedelta(hours=8))
MONITOR_STATE_VERSION = 2

# 收件人邮箱（可按需修改）
DEFAULT_RECIPIENT = "shangyinan@ucas.com.cn"

# 平台标签
PLATFORM_LABEL = {
    "douyin": "抖音", "bilibili": "B站", "wechat_channels": "视频号",
    "csdn": "CSDN", "elecfans": "电子发烧友", "baijiahao": "百家号",
    "zhihu": "知乎", "wechat_service": "公众号", "wechat_subscription": "公众号",
    "toutiao": "今日头条", "sohu": "搜狐", "xiaohongshu": "小红书",
}

# 可通过 平台评论数据源抓正文的平台 -> 接口配置
#   adapter: 平台标识
#   kind: 内容ID类型 (用于构造请求)
#   type: 内容类型标签
COMMENT_API_PLATFORMS = {
    "douyin": {
        "path": "douyin.fetch_video_comments",
        "method": "get",
        "params": lambda item: {"aweme_id": item["content_id"], "cursor": 0, "count": 20},
        "type": "视频",
    },
    "bilibili": {
        "path": "bilibili.fetch_video_comments",
        "method": "get",
        "params": lambda item: {"bv_id": item["content_id"], "mode": 3, "next_offset": 1, "ps": 20},
        "type": "视频",
    },
    "xiaohongshu": {
        "path": "xiaohongshu.get_note_comments",
        "method": "get",
        "params": lambda item: {"note_id": item["content_id"],
                                "cursor": "", "index": 0,
                                "pageArea": "UNFOLDED", "sort_strategy": "latest_v2"},
        "type": "笔记",
    },
    "wechat_channels": {
        "path": "wechat_channels.fetch_video_comments",
        "method": "post",
        "params": lambda item: {"object_id": item["content_id"], "last_buffer": "",
                                "comment_id": "", "raw": False},
        "type": "视频",
    },
}





def to_int(v, default=None):
    try:
        return int(str(v).replace(",", "").replace("+", ""))
    except (TypeError, ValueError):
        return default


def dig(obj, *paths, default=None):
    for path in paths:
        cur = obj
        ok = True
        for key in path.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            elif isinstance(cur, list) and key.isdigit() and int(key) < len(cur):
                cur = cur[int(key)]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default





def unwrap_data(obj):
    """解开 历史外层包装，找到真正的 data 节点。"""
    cur = obj
    for _ in range(5):
        if not isinstance(cur, dict) or not isinstance(cur.get("data"), dict):
            break
        if "request_id" in cur or "router" in cur or \
                set(cur).issubset({"code", "message", "ttl", "data"}):
            cur = cur["data"]
        else:
            break
    return cur


def load_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# 内容清单构建：从大屏数据提取所有内容
# ---------------------------------------------------------------------------

def comment_count_metadata(record):
    provenance = dig(record, "metric_provenance.comment", default={})
    provenance = provenance if isinstance(provenance, dict) else {}
    return {
        "data_source": record.get("data_source") or "",
        "comment_definition": provenance.get("definition") or "",
        "comment_metric_source": provenance.get("source") or "",
        "snapshot_state": record.get("snapshot_state") or "",
        "comment_is_cached": (provenance.get("source") == "cached"
                              or "stats.comment" in (record.get("cached_fields") or [])),
    }


def build_content_list(max_age_days=0):
    """从两个大屏 JSON 构建统一内容清单。

    返回: list of dict
        {account_key, platform, platform_label, account_name, business_line,
         content_id, title, url, published_at, content_type, stats_comment}
    """
    contents = []
    video_data = load_json(VIDEO_DATA) or {}
    for v in video_data.get("videos", []):
        contents.append({
            "account_key": v.get("account_key", ""),
            "platform": v.get("platform", ""),
            "platform_label": v.get("platform_label", ""),
            "account_name": v.get("account_name", ""),
            "business_line": v.get("business_line", ""),
            "content_id": str(v.get("video_id", "") or ""),
            "aid": str(v.get("aid", "") or ""),
            "title": v.get("title", ""),
            "url": v.get("url", ""),
            "published_at": v.get("published_at", ""),
            "content_type": "视频",
            "stats_comment": to_int(dig(v, "stats.comment")),
            "primary_account_key": v.get("primary_account_key", ""),
            **comment_count_metadata(v),
        })

    article_data = load_json(ARTICLE_DATA) or {}
    for a in article_data.get("articles", []):
        contents.append({
            "account_key": a.get("account_key", ""),
            "platform": a.get("platform", ""),
            "platform_label": a.get("platform_label", ""),
            "account_name": a.get("account_name", ""),
            "business_line": a.get("business_line", ""),
            "content_id": str(a.get("article_id", "") or ""),
            "aid": "",
            "title": a.get("title", ""),
            "url": a.get("url", ""),
            "published_at": a.get("published_at", ""),
            "content_type": a.get("content_type", "图文"),
            "stats_comment": to_int(dig(a, "stats.comment")),
            **comment_count_metadata(a),
        })

    # 过滤：无 ID、时间过早、无 URL（评论需要可访问的内容）
    cutoff = (datetime.now(CN_TZ) - timedelta(days=max_age_days)
              if max_age_days > 0 else None)
    filtered = []
    for c in contents:
        if not c["content_id"] or c["content_id"] in ("None", "0"):
            continue
        if c.get("primary_account_key") \
                and c.get("account_key") != c.get("primary_account_key"):
            continue
        try:
            pub = datetime.fromisoformat(c["published_at"].replace("Z", "+00:00"))
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=CN_TZ)
            pub = pub.astimezone(CN_TZ)
            if cutoff is not None and pub < cutoff:
                continue
        except (ValueError, TypeError):
            pass
        filtered.append(c)
    return filtered


def epoch_to_iso(ts):
    ts = to_int(ts)
    if not ts:
        return None
    if ts > 10_000_000_000:
        ts //= 1000
    try:
        return datetime.fromtimestamp(ts, tz=CN_TZ).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _account_record(account, content_id, *, title="", url="", published_at=None,
                    comment_count=None, aid=""):
    return {
        "account_key": f"{account['platform']}:{account['account_name']}",
        "platform": account["platform"],
        "platform_label": PLATFORM_LABEL.get(account["platform"], account["platform"]),
        "account_name": account["account_name"],
        "business_line": account.get("business_line", ""),
        "content_id": str(content_id),
        "aid": str(aid or ""),
        "title": title or "(新发现内容)",
        "url": url or "",
        "published_at": published_at or "",
        "content_type": account.get("content_type") or (
            "笔记" if account["platform"] == "xiaohongshu" else "视频"),
        "stats_comment": to_int(comment_count),
        "newly_discovered": True,
    }


def _deepest_data(payload):
    current = unwrap_data(payload)
    for _ in range(3):
        if not isinstance(current, dict) or not isinstance(current.get("data"), dict):
            break
        current = current["data"]
    return current if isinstance(current, dict) else {}


def discover_latest_contents(client, existing, platform_filter=None):
    """Reuse the same provider for hourly discovery; no independent paid fallback."""
    known = {(x["platform"], x["content_id"]) for x in existing}
    additions, errors = [], []
    for path in (VIDEO_ACCOUNTS, ARTICLE_ACCOUNTS):
        for account in (load_json(path) or {}).get("accounts", []):
            if account.get("platform") not in COMMENT_API_PLATFORMS:
                continue
            if platform_filter and not ({account["platform"], account["account_name"],
                                         f"{account['platform']}:{account['account_name']}",
                                         PLATFORM_LABEL.get(account["platform"], "")} & set(platform_filter)):
                continue
            try:
                result = client.discover(account)
                for raw in result.records:
                    cid = str(raw.get("video_id") or raw.get("article_id") or "")
                    identity = (account["platform"], cid)
                    if not cid or identity in known:
                        continue
                    known.add(identity)
                    additions.append(_account_record(account, cid, aid=raw.get("aid", ""),
                        title=raw.get("title", ""), url=raw.get("url", ""),
                        published_at=raw.get("published_at"),
                        comment_count=raw.get("stats", {}).get("comment")))
            except Exception as exc:
                errors.append(f"{account['account_name']} 新作品发现失败: {compact_error(exc, 180)}")
    return existing + additions, additions, errors


# ---------------------------------------------------------------------------
# 评论正文解析：各平台从响应中提取评论列表
# ---------------------------------------------------------------------------

def identity_values(*values):
    result = []
    for value in values:
        candidates = value if isinstance(value, (list, tuple, set)) else [value]
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text and text not in result:
                result.append(text)
    return result


def parse_douyin_comments(data):
    data = unwrap_data(data)
    comments = dig(data, "comments", "data.comments", default=[]) or []
    out = []
    for c in comments:
        cid = str(dig(c, "cid", "comment_id", "id", default=""))
        if not cid:
            continue
        user = c.get("user", {}) or {}
        if not isinstance(user, dict):
            user = {}
        out.append({
            "comment_id": cid,
            "content": dig(c, "text", "content", default="") or "",
            "user": user.get("nickname") or c.get("nickname") or "",
            "user_ids": identity_values(
                user.get("uid"), user.get("sec_uid"), user.get("unique_id"),
                user.get("short_id"), c.get("user_id")),
            "like": to_int(dig(c, "digg_count", "like_count", default=0), 0),
            "time": epoch_to_str(dig(c, "create_time", "createTime")),
            "created_at": epoch_to_iso(dig(c, "create_time", "createTime")),
            "reply_count": to_int(dig(c, "reply_comment_total", "reply_count"), 0),
        })
    return out


def parse_bilibili_comments(data):
    data = unwrap_data(data)
    replies = dig(
        data, "data.replies", "replies", "data.reply", "root.replies", default=[]) or []
    top_replies = dig(data, "top_replies", "data.top_replies", default=[]) or []
    if isinstance(top_replies, list):
        replies = list(replies) + top_replies
    out = []
    for c in replies:
        if not isinstance(c, dict):
            continue
        cid = str(dig(c, "rpid", "id", "reply_id", default=""))
        if not cid:
            continue
        member = c.get("member", {}) or {}
        out.append({
            "comment_id": cid,
            "content": dig(c, "content.message", "content", "message", default="") or "",
            "user": member.get("uname", "") or "",
            "user_ids": identity_values(member.get("mid"), c.get("mid")),
            "like": to_int(dig(c, "like", "like_count", default=0), 0),
            "time": epoch_to_str(dig(c, "ctime", "create_time")),
            "created_at": epoch_to_iso(dig(c, "ctime", "create_time")),
            "reply_count": to_int(dig(c, "rcount", "count"), 0),
        })
    return out


def parse_xiaohongshu_comments(data):
    data = unwrap_data(data)
    root = data.get("data", data) if isinstance(data, dict) else {}
    comments = dig(root, "comments", "note_comments", default=[]) or []
    out = []
    for c in comments:
        cid = str(dig(c, "comment_id", "id", "cid", default=""))
        if not cid:
            continue
        user = c.get("user", {}) or {}
        if not isinstance(user, dict):
            user = {}
        out.append({
            "comment_id": cid,
            "content": dig(c, "content", "text", default="") or "",
            "user": user.get("nickname", "") or "",
            "user_ids": identity_values(
                user.get("user_id"), user.get("userid"), user.get("id"),
                user.get("red_id"), c.get("user_id")),
            "like": to_int(dig(c, "like_count", "likeCount", default=0), 0),
            "time": epoch_to_str(dig(c, "create_time", "time")),
            "created_at": epoch_to_iso(dig(c, "create_time", "time")),
            "reply_count": to_int(dig(c, "sub_comment_count", "reply_count"), 0),
        })
    return out


def parse_wechat_channels_comments(data):
    data = unwrap_data(data)
    comments = dig(data, "comments", "data.comments", default=[]) or []
    out = []
    for c in comments:
        cid = str(dig(c, "comment_id", "commentId", "id", default=""))
        if not cid:
            continue
        user = c.get("user", {}) or {}
        if not isinstance(user, dict):
            user = {}
        out.append({
            "comment_id": cid,
            "content": dig(c, "content", default="") or "",
            "user": dig(c, "nickname", "username", default="") or "",
            "user_ids": identity_values(
                c.get("username"), c.get("user_id"), c.get("finder_username"),
                user.get("username"), user.get("user_id"),
                dig(c, "contact.username", "reply_contact.username")),
            "like": to_int(dig(c, "like_count", "likeCount", default=0), 0),
            "time": epoch_to_str(dig(c, "create_time", "createtime")),
            "created_at": epoch_to_iso(dig(c, "create_time", "createtime")),
            "reply_count": to_int(dig(c, "reply_count", "replyCount"), 0),
        })
    return out


PARSERS = {
    "douyin": parse_douyin_comments,
    "bilibili": parse_bilibili_comments,
    "xiaohongshu": parse_xiaohongshu_comments,
    "wechat_channels": parse_wechat_channels_comments,
}


def _normalized_identity(value):
    return str(value or "").strip().casefold()


def load_official_identities():
    """构建账号主键 -> 平台稳定用户 ID 集合，不使用昵称猜测官方身份。"""
    result = {}

    def add(account):
        platform = str(account.get("platform") or "")
        account_name = str(account.get("account_name") or "")
        key = str(account.get("account_key") or f"{platform}:{account_name}")
        if platform not in COMMENT_API_PLATFORMS or not account_name:
            return
        entry = result.setdefault(key, {"platform": platform, "ids": set()})
        for field in ("platform_uid", "provided_id", "official_user_id"):
            value = _normalized_identity(account.get(field))
            if value:
                entry["ids"].add(value)

    for path in (VIDEO_ACCOUNTS, ARTICLE_ACCOUNTS):
        for account in (load_json(path) or {}).get("accounts", []):
            add(account)
    for path in (VIDEO_DATA, ARTICLE_DATA):
        for account in (load_json(path) or {}).get("accounts", []):
            add(account)
    return result


def is_official_author(comment, item, official_identities) -> bool:
    expected = official_identities.get(item.get("account_key"), {}).get("ids", set())
    actual = {_normalized_identity(value) for value in comment.get("user_ids", [])}
    actual.discard("")
    return bool(expected and actual.intersection(expected))


def refresh_verified_official_identity(client, item, official_identities):
    """Use the successful native scan's identity even before a dashboard exists."""
    from providers.base import ProviderError, identifier
    from providers.douyin import DouyinProvider
    from providers.wechat_channels import WeChatChannelsProvider

    if not isinstance(client, ProviderRegistry):
        return
    provider = client.get(item)
    if not isinstance(provider, (DouyinProvider, WeChatChannelsProvider)):
        return
    # Only _profile's successful login check publishes this value. A configured
    # binding alone is not proof that the current session belongs to it. For
    # Channels, fetch_roots must also finish the request's finder-ID check.
    profile = getattr(provider, "verified_profile", {})
    handle = identifier(profile.get("verified_account_id"))
    uid = identifier(profile.get("official_user_id"))
    if isinstance(provider, DouyinProvider):
        platform = "douyin"
        if (not handle or handle != identifier(provider.account.get("platform_uid"))
                or not uid.isdigit()
                or (provider.settings.get("expected_uid")
                    and uid != identifier(provider.settings["expected_uid"]))):
            raise ProviderError("identity_mismatch", "评论采集缺少本次核验的抖音作者身份")
    else:
        platform = "wechat_channels"
        canonical = identifier(provider.account.get("platform_uid"))
        bound_sph = identifier(provider.settings.get("expected_sph"))
        if (not handle or handle != (canonical or bound_sph)
                or (bound_sph and handle != bound_sph)
                or not uid or uid != identifier(provider.settings.get("expected_finder_id"))):
            raise ProviderError("identity_mismatch", "评论采集缺少本次核验的视频号作者身份")
    # Replace potentially stale snapshot identities, rather than continuing to
    # recognize an old account after an explicitly configured account switch.
    official_identities[item["account_key"]] = {
        "platform": platform, "ids": {_normalized_identity(handle), _normalized_identity(uid)},
    }


def _parse_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def content_poll_hours(item, args, *, now=None) -> int:
    now = (now or datetime.now(CN_TZ)).astimezone(CN_TZ)
    published = _parse_iso(item.get("published_at"))
    age_days = max(0, (now - published).days) if published else 10_000
    fresh_days = int(getattr(args, "fresh_days", 7))
    recent_days = int(getattr(args, "recent_days", 30))
    if age_days <= fresh_days:
        return max(1, int(getattr(args, "fresh_hours", 2)))
    if age_days <= recent_days:
        return max(1, int(getattr(args, "recent_hours", 6)))
    return max(1, int(getattr(args, "older_hours", 24)))


def content_is_due(item, state, args, *, now=None) -> bool:
    if not getattr(args, "tiered_polling", False) or item.get("newly_discovered"):
        return True
    now = (now or datetime.now(CN_TZ)).astimezone(CN_TZ)
    key = f"{item.get('platform')}:{item.get('content_id')}"
    last = _parse_iso((state.get("content_poll_at") or {}).get(key))
    if not last:
        return True
    return now - last >= timedelta(hours=content_poll_hours(item, args, now=now))


def discovery_is_due(state, args, *, now=None) -> bool:
    if getattr(args, "no_discovery", False):
        return False
    now = (now or datetime.now(CN_TZ)).astimezone(CN_TZ)
    last = _parse_iso(state.get("last_discovery_attempt_at"))
    interval = max(1, int(getattr(args, "discovery_hours", 2)))
    return not last or now - last >= timedelta(hours=interval)


def merge_cached_discoveries(contents, state, max_age_days, *, now=None):
    now = (now or datetime.now(CN_TZ)).astimezone(CN_TZ)
    cutoff = now - timedelta(days=max_age_days) if max_age_days > 0 else None
    result = list(contents)
    known = {(item.get("platform"), item.get("content_id")) for item in result}
    retained = []
    for item in state.get("discovered_contents", []):
        item = {**item, "newly_discovered": False}
        published = _parse_iso(item.get("published_at"))
        if cutoff and published and published < cutoff:
            continue
        retained.append(item)
        identity = (item.get("platform"), item.get("content_id"))
        if identity not in known:
            result.append(item)
            known.add(identity)
    state["discovered_contents"] = retained
    return result


def cache_discoveries(state, additions):
    merged = {
        (item.get("platform"), item.get("content_id")): dict(item)
        for item in state.get("discovered_contents", [])
        if item.get("platform") and item.get("content_id")
    }
    for item in additions:
        merged[(item.get("platform"), item.get("content_id"))] = {
            **item, "newly_discovered": False,
        }
    state["discovered_contents"] = list(merged.values())


def epoch_to_str(ts):
    """Unix 秒/毫秒时间戳 -> 本地时间字符串。"""
    ts = to_int(ts)
    if not ts:
        return ""
    if ts > 10_000_000_000:
        ts //= 1000
    try:
        return datetime.fromtimestamp(ts, tz=CN_TZ).strftime("%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# 状态管理
# ---------------------------------------------------------------------------

def load_state():
    state = load_json(STATE_PATH)
    if not isinstance(state, dict):
        state = {}
    # 状态文件不存在 → 首次运行，尚未建立基线
    first_run = not STATE_PATH.exists()
    if "seen_comments" not in state:
        state["seen_comments"] = {}
    if "content_counts" not in state:
        state["content_counts"] = {}
    if "content_count_origins" not in state:
        state["content_count_origins"] = {}
    if "content_poll_at" not in state:
        state["content_poll_at"] = {}
    if "root_reply_counts" not in state:
        state["root_reply_counts"] = {}
    if "discovered_contents" not in state:
        state["discovered_contents"] = []
    if "baseline_done" not in state:
        state["baseline_done"] = not first_run
    if state.get("monitor_state_version") != MONITOR_STATE_VERSION:
        # 语义升级：从本次正式启动重新建立“只提醒启动后评论”的基线。
        # 保留历史已见ID用于去重，但清空旧版不完整分页/计数基线。
        state["monitor_state_version"] = MONITOR_STATE_VERSION
        state["monitor_started_at"] = datetime.now(CN_TZ).isoformat()
        state["baseline_done"] = False
        state["full_scan_baselines"] = []
        state["content_counts"] = {}
        state["content_count_origins"] = {}
    return state


def save_state(state):
    atomic_write_json(STATE_PATH, state, pretty=True)


# ---------------------------------------------------------------------------
# 邮件正文生成
# ---------------------------------------------------------------------------

def build_email_text(new_items):
    """把新增评论汇总成邮件正文（纯文本）。"""
    lines = []
    lines.append("检测到以下内容有新的评论：")
    lines.append("")
    for item in new_items:
        lines.append(f"【{item['platform_label']} / {item['account_name']}】")
        title = item.get("title") or "(无标题)"
        if len(title) > 40:
            title = title[:40] + "…"
        lines.append(f"  内容：{title}")
        lines.append(f"  链接：{item.get('url') or item.get('content_id', '')}")
        if item.get("comments"):
            lines.append(f"  新增 {len(item['comments'])} 条评论：")
            for c in item["comments"]:
                user = c.get("user") or "匿名用户"
                content = (c.get("content") or "").strip().replace("\n", " ")
                if len(content) > 80:
                    content = content[:80] + "…"
                lines.append(f"    · {user}：{content}")
        else:
            lines.append(f"  新增 {item.get('added_count', 0)} 条评论")
        lines.append("")
    lines.append("—— 视频推广数据大屏 · 评论监控")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主检查逻辑
# ---------------------------------------------------------------------------

def comment_created_after(comment, started_at):
    """首次发现内容时，仅放行有可靠时间且不早于服务基线的评论。"""
    value = comment.get("created_at")
    if not value or not started_at:
        return False
    try:
        created = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=CN_TZ)
        if started.tzinfo is None:
            started = started.replace(tzinfo=CN_TZ)
        return created.astimezone(CN_TZ) >= started.astimezone(CN_TZ)
    except (TypeError, ValueError):
        return False


def check_comments(client, contents, args, *, state=None, timeline=None,
                   official_identities=None, now=None):
    """执行一轮分层评论检查，仅在回复数增长时查询二级回复。"""
    state = state if isinstance(state, dict) else load_state()
    timeline = timeline if isinstance(timeline, dict) else None
    official_identities = official_identities or {}
    now = (now or datetime.now(CN_TZ)).astimezone(CN_TZ)
    now_iso = now.isoformat()
    seen = state.get("seen_comments", {})
    legacy_channel_comments = {
        str(comment_id) for key, ids in seen.items()
        if key.startswith("wechat_channels:") and not key.startswith("wechat_channels:export/")
        for comment_id in ids
    }
    counts = state.get("content_counts", {})
    count_origins = state.get("content_count_origins", {})
    poll_at = state.get("content_poll_at", {})
    reply_counts = state.get("root_reply_counts", {})
    full_scan_baselines = set(state.get("full_scan_baselines", []))
    new_items = []
    errors = []
    scan_totals = {
        "detail_contents": 0, "root_pages": 0, "reply_pages": 0,
        "public_comments": 0, "skipped_not_due": 0,
        "reply_threads_checked": 0, "timeline_comments_added": 0,
        "official_replies_added": 0, "unidentified_reply_authors": 0,
        "budget_exhausted": False,
    }
    calls_before = getattr(client, "call_count", 0) if client else 0

    by_account = {}
    for content in contents:
        by_account.setdefault(content["account_key"], []).append(content)
    for key in by_account:
        by_account[key].sort(
            key=lambda value: value.get("published_at") or "", reverse=True)

    platform_filter = set(args.platform or [])
    total_new = 0
    stop_for_budget = False

    for account_key, items in by_account.items():
        if stop_for_budget:
            break
        platform = items[0]["platform"]
        label = items[0]["platform_label"] or PLATFORM_LABEL.get(platform, platform)
        account_name = items[0]["account_name"]
        if platform_filter and platform not in platform_filter \
                and label not in platform_filter and account_key not in platform_filter:
            continue
        recent = items if args.limit <= 0 else items[: args.limit]
        scope_text = "全部" if args.limit <= 0 else "最近"
        print(f">>> {label} / {account_name}：候选{scope_text} {len(recent)} 条内容")

        for item in recent:
            if stop_for_budget:
                break
            cid = item["content_id"]
            key_of_cid = f"{platform}:{cid}"

            if platform in COMMENT_API_PLATFORMS and client:
                if not content_is_due(item, state, args, now=now):
                    scan_totals["skipped_not_due"] += 1
                    continue
                try:
                    roots, root_pages = fetch_root_comments(
                        client, platform, item, max_pages=args.max_pages)
                    refresh_verified_official_identity(client, item, official_identities)
                    errors_before_content = len(errors)
                    scan_totals["detail_contents"] += 1
                    scan_totals["root_pages"] += root_pages
                    scan_totals["public_comments"] += len(roots)

                    audience_roots = []
                    for root in roots:
                        official_root = is_official_author(
                            root, item, official_identities)
                        if not official_root:
                            audience_roots.append(root)
                            if timeline and timeline_store.record_comment(
                                    timeline, item, root, observed_at=now_iso):
                                scan_totals["timeline_comments_added"] += 1

                        reply_key = f"{key_of_cid}:{root['comment_id']}"
                        current_replies = max(0, to_int(root.get("reply_count"), 0))
                        previous_replies = reply_counts.get(reply_key)
                        root_tracked = bool(
                            timeline and timeline_store.event_key(
                                platform, str(root["comment_id"]))
                            in timeline.get("events", {}))
                        should_fetch_replies = (
                            not args.no_replies and root_tracked and current_replies > 0
                            and (previous_replies is None
                                 or current_replies > int(previous_replies)
                                 or root.get("reply_count_is_lower_bound", False)))
                        if not should_fetch_replies:
                            if previous_replies is None:
                                reply_counts[reply_key] = current_replies
                            elif current_replies > int(previous_replies):
                                # 显式关闭回复时仍推进计数基线，避免恢复后回补历史。
                                reply_counts[reply_key] = current_replies
                            continue

                        try:
                            replies, reply_pages = fetch_comment_replies(
                                client, platform, item, str(root["comment_id"]),
                                max_pages=args.max_pages)
                            scan_totals["reply_threads_checked"] += 1
                            scan_totals["reply_pages"] += reply_pages
                            scan_totals["public_comments"] += len(replies)
                            for reply in replies:
                                if not reply.get("user_ids"):
                                    scan_totals["unidentified_reply_authors"] += 1
                                if is_official_author(
                                        reply, item, official_identities) and timeline:
                                    if timeline_store.record_official_reply(
                                            timeline, item, root, reply,
                                            observed_at=now_iso):
                                        scan_totals["official_replies_added"] += 1
                            reply_counts[reply_key] = max(
                                current_replies, len(replies), int(previous_replies or 0))
                        except ApiBudgetExceeded:
                            raise
                        except Exception as exc:
                            errors.append(
                                f"{label} {cid} 回复 {root['comment_id']}: "
                                f"{compact_error(exc, 100)}")

                    if len(errors) == errors_before_content:
                        poll_at[key_of_cid] = now_iso
                    all_ids = {comment["comment_id"] for comment in roots}
                    previously_seen = set(seen.get(key_of_cid, []))
                    if key_of_cid not in full_scan_baselines:
                        seen[key_of_cid] = sorted(previously_seen | all_ids)
                        full_scan_baselines.add(key_of_cid)
                        if not state.get("baseline_done", False):
                            if roots:
                                print(
                                    f"    启动基线：{len(roots)} 条公开一级评论，"
                                    f"共 {root_pages} 页")
                            continue
                        new_comments = [
                            comment for comment in audience_roots
                            if comment_created_after(
                                comment, state.get("monitor_started_at"))
                            and not (platform == "wechat_channels" and cid.startswith("export/")
                                     and comment["comment_id"] in legacy_channel_comments)]
                    else:
                        new_comments = [
                            comment for comment in audience_roots
                            if comment["comment_id"] not in previously_seen]
                        seen[key_of_cid] = sorted(previously_seen | all_ids)
                    if new_comments:
                        entry = dict(item)
                        entry["comments"] = new_comments
                        entry["scan_pages"] = {
                            "root_pages": root_pages,
                            "reply_pages": 0,
                            "comments": len(roots),
                        }
                        entry["platform_label"] = label
                        new_items.append(entry)
                        total_new += len(new_comments)
                except ApiBudgetExceeded as exc:
                    errors.append(str(exc))
                    scan_totals["budget_exhausted"] = True
                    stop_for_budget = True
                except Exception as exc:
                    errors.append(f"{label} {cid}: {compact_error(exc, 120)}")
            elif platform in COMMENT_API_PLATFORMS \
                    and not (getattr(args, "dry_run", False)
                             or getattr(args, "no_api", False)):
                errors.append(f"{label}: 未配置平台数据源，无法检查评论明细")
            else:
                current = item.get("stats_comment")
                count_key = key_of_cid
                comparable = True
                if platform not in COMMENT_API_PLATFORMS:
                    # Message IDs such as公众号 mid-idx are not unique across
                    # accounts. Never infer an owner for legacy platform-only keys.
                    owner = item.get("account_key") or ""
                    if not owner.startswith(platform + ":") or not owner[len(platform) + 1:]:
                        continue
                    if (item.get("snapshot_state") == "cached" or item.get("comment_is_cached")
                            or item.get("comment_metric_source") == "cached" or current is None):
                        continue
                    count_key = f"{owner}:{cid}"
                    origin = {field: item.get(field) or "" for field in
                              ("data_source", "comment_definition", "comment_metric_source")}
                    # Missing metadata remains compatible only with another
                    # missing-metadata sample under this exact account key.
                    previous_origin = count_origins.get(count_key, dict.fromkeys(origin, ""))
                    comparable = previous_origin == origin
                    count_origins[count_key] = origin
                last = counts.get(count_key)
                if comparable and current is not None and last is not None and current > last:
                    entry = dict(item)
                    entry["comments"] = []
                    entry["added_count"] = current - last
                    entry["platform_label"] = label
                    new_items.append(entry)
                    total_new += entry["added_count"]
                if current is not None:
                    counts[count_key] = current

    state["seen_comments"] = {key: list(value) for key, value in seen.items()}
    state["content_counts"] = counts
    state["content_count_origins"] = count_origins
    state["content_poll_at"] = poll_at
    state["root_reply_counts"] = reply_counts
    state["full_scan_baselines"] = sorted(full_scan_baselines)
    state["last_checked_at"] = now_iso
    state["last_scan"] = {
        **scan_totals,
        "api_calls": (getattr(client, "call_count", 0) - calls_before) if client else 0,
        "errors": len(errors),
        "complete": not errors,
    }
    if total_new > 0:
        state["last_new_count"] = total_new
    return new_items, errors, state


REPLY_API_PLATFORMS = {
    "douyin": {
        "path": "douyin.fetch_video_comment_replies",
        "method": "get",
    },
    "bilibili": {
        "path": "bilibili.fetch_reply_detail",
        "method": "get",
    },
    "xiaohongshu": {
        "path": "xiaohongshu.get_note_sub_comments",
        "method": "get",
    },
    "wechat_channels": {
        "path": "wechat_channels.fetch_video_comments",
        "method": "post",
    },
}


def _content_identity_params(platform, item):
    """返回平台内容ID参数，B站优先使用精度稳定的 AV ID。"""
    if platform == "bilibili":
        aid = str(item.get("aid") or "")
        if aid and aid != "None":
            return {"av_id": aid}
        return {"bv_id": item["content_id"]}
    return {}


def _initial_root_request(platform, item):
    if platform == "douyin":
        return {"aweme_id": item["content_id"], "cursor": 0, "count": 20}
    if platform == "bilibili":
        return {**_content_identity_params(platform, item), "mode": 3, "next_offset": 1}
    if platform == "xiaohongshu":
        return {
            "note_id": item["content_id"], "cursor": "", "index": 0,
            "pageArea": "UNFOLDED", "sort_strategy": "latest_v2",
        }
    if platform == "wechat_channels":
        return {
            "object_id": item["content_id"], "last_buffer": "",
            "comment_id": "", "raw": False,
        }
    raise ValueError(f"不支持评论明细接口的平台: {platform}")


def _initial_reply_request(platform, item, root_id):
    if platform == "douyin":
        return {
            "item_id": item["content_id"], "comment_id": root_id,
            "cursor": 0, "count": 20,
        }
    if platform == "bilibili":
        return {
            **_content_identity_params(platform, item), "root": root_id,
            "next_offset": 0, "ps": 20,
        }
    if platform == "xiaohongshu":
        return {
            "note_id": item["content_id"], "comment_id": root_id,
            "cursor": "", "index": 1,
        }
    if platform == "wechat_channels":
        return {
            "object_id": item["content_id"], "last_buffer": "",
            "comment_id": root_id, "raw": False,
        }
    raise ValueError(f"不支持评论回复接口的平台: {platform}")


def _page_data(platform, response):
    data = unwrap_data(response)
    if platform == "xiaohongshu" and isinstance(data, dict) \
            and isinstance(data.get("data"), dict):
        return data["data"]
    return data if isinstance(data, dict) else {}


def _cursor_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _next_page_params(platform, response, current):
    """返回下一页参数；None 表示完整结束，有下一页但无游标则抛错。"""
    data = _page_data(platform, response)
    next_params = dict(current)

    if platform == "douyin":
        if not bool(data.get("has_more")):
            return None
        cursor = data.get("cursor")
        if cursor in (None, "", current.get("cursor")):
            raise RuntimeError("抖音响应声明有下一页，但未返回新 cursor")
        next_params["cursor"] = cursor
        return next_params

    if platform == "bilibili":
        cursor = data.get("cursor") or {}
        if bool(cursor.get("is_end")):
            return None
        # 接口 next_offset 参数是整数。pagination_reply.next_offset 是内部
        # base64 token，直接回传会得到 HTTP 422。
        next_offset = cursor.get("next")
        if not isinstance(next_offset, int):
            candidate = dig(cursor, "pagination_reply.next_offset")
            next_offset = candidate if isinstance(candidate, int) else None
        if next_offset in (None, "", current.get("next_offset")):
            raise RuntimeError("B站响应未结束，但未返回新 next_offset")
        next_params["next_offset"] = next_offset
        return next_params

    if platform == "xiaohongshu":
        if not bool(data.get("has_more")):
            return None
        cursor = _cursor_object(data.get("cursor"))
        cursor_value = cursor.get("cursor", data.get("cursor"))
        index = cursor.get("index", data.get("index"))
        page_area = cursor.get("pageArea", data.get("pageArea"))
        if cursor_value in (None, "", current.get("cursor")) and index in (
                None, current.get("index")):
            raise RuntimeError("小红书响应声明有下一页，但未返回新 cursor/index")
        if cursor_value is not None:
            next_params["cursor"] = cursor_value
        if index is not None:
            next_params["index"] = index
        if page_area:
            next_params["pageArea"] = page_area
        return next_params

    if platform == "wechat_channels":
        if not bool(data.get("down_continue")):
            return None
        last_buffer = data.get("last_buffer")
        if last_buffer in (None, "", current.get("last_buffer")):
            raise RuntimeError("视频号响应声明有下一页，但未返回新 last_buffer")
        next_params["last_buffer"] = last_buffer
        return next_params

    raise ValueError(f"不支持分页的平台: {platform}")


def _request_comment_page(client, endpoint, params, usage_task):
    if endpoint["method"] == "post":
        return client.request(
            "POST", endpoint["path"], payload=params, usage_task=usage_task)
    return client.request(
        "GET", endpoint["path"], params=params, usage_task=usage_task)


def _validate_comment_page(platform, response):
    """HTTP 200 不等于上游成功，识别平台包装中的业务错误。"""
    if not isinstance(response, dict):
        raise RuntimeError("评论接口未返回 JSON 对象")
    outer_code = response.get("code")
    if outer_code not in (None, 0, 200, "0", "200"):
        raise RuntimeError(f"评论接口业务错误 code={outer_code}")
    data = unwrap_data(response)
    if isinstance(data, dict):
        status_code = data.get("status_code")
        if status_code not in (None, 0, "0"):
            raise RuntimeError(f"评论上游错误 status_code={status_code}")
        if platform == "douyin":
            fatal_ids = dig(data, "extra.fatal_item_ids", default=[]) or []
            if fatal_ids:
                raise RuntimeError("抖音评论当前不可公开获取（fatal_item_ids）")
        if platform == "xiaohongshu":
            if data.get("success") is False:
                raise RuntimeError(
                    f"小红书评论上游失败: {compact_error(data.get('msg', '未知错误'), 80)}")
            inner = data.get("data") if isinstance(data.get("data"), dict) else {}
            inner_code = inner.get("code")
            if inner_code not in (None, 0, 200, "0", "200"):
                raise RuntimeError(f"小红书评论上游错误 code={inner_code}")


def _dedupe_comments(comments):
    result = []
    seen = set()
    for comment in comments:
        comment_id = str(comment.get("comment_id") or "")
        if not comment_id or comment_id in seen:
            continue
        seen.add(comment_id)
        result.append(comment)
    return result


def _fetch_pages(client, platform, endpoint, initial_params, max_pages, parent_id="",
                 usage_task="comment_roots"):
    comments = []
    params = initial_params
    page_count = 0
    visited = set()
    while True:
        fingerprint = json.dumps(params, ensure_ascii=False, sort_keys=True)
        if fingerprint in visited:
            raise RuntimeError(f"{PLATFORM_LABEL.get(platform, platform)} 分页游标重复")
        visited.add(fingerprint)
        response = _request_comment_page(client, endpoint, params, usage_task)
        _validate_comment_page(platform, response)
        page_count += 1
        page_comments = PARSERS[platform](response)
        if parent_id:
            for comment in page_comments:
                comment["parent_comment_id"] = parent_id
        comments.extend(page_comments)
        next_params = _next_page_params(platform, response, params)
        if next_params is None:
            return _dedupe_comments(comments), page_count
        if page_count >= max_pages:
            raise RuntimeError(
                f"{PLATFORM_LABEL.get(platform, platform)} 评论超过 {max_pages} 页，"
                "为防止异常循环已停止；本轮不推进评论状态")
        params = next_params


def fetch_root_comments(client, platform, item, max_pages=200):
    if hasattr(client, "fetch_roots"):
        return client.fetch_roots(item, max_pages)
    return _fetch_pages(
        client, platform, COMMENT_API_PLATFORMS[platform],
        _initial_root_request(platform, item), max_pages,
        usage_task="comment_roots")


def fetch_comment_replies(client, platform, item, root_id, max_pages=200):
    if hasattr(client, "fetch_replies"):
        return client.fetch_replies(item, root_id, max_pages)
    return _fetch_pages(
        client, platform, REPLY_API_PLATFORMS[platform],
        _initial_reply_request(platform, item, root_id), max_pages,
        parent_id=root_id, usage_task="comment_replies")


def fetch_all_comments(client, platform, item, max_pages=200, include_replies=True):
    """兼容全量入口；正式监控使用回复数变化驱动的条件采集。"""
    roots, pages = fetch_root_comments(client, platform, item, max_pages=max_pages)
    comments = list(roots)
    reply_pages = 0
    if include_replies:
        for root in roots:
            if to_int(root.get("reply_count"), 0) <= 0:
                continue
            root_id = str(root["comment_id"])
            replies, used_pages = fetch_comment_replies(
                client, platform, item, root_id, max_pages=max_pages)
            comments.extend(replies)
            reply_pages += used_pages
    return _dedupe_comments(comments), {
        "root_pages": pages,
        "reply_pages": reply_pages,
        "comments": len(comments),
    }


def fetch_and_diff_comments(client, platform, item, seen_ids, max_pages=200,
                            include_replies=True):
    """兼容入口：完整分页后返回未见评论。"""
    comments, _stats = fetch_all_comments(
        client, platform, item, max_pages=max_pages,
        include_replies=include_replies)
    seen = set(seen_ids or [])
    return [comment for comment in comments if comment["comment_id"] not in seen]


def compact_error(exc, limit=300):
    text = re.sub(r"\s+", " ", str(exc)).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ---------------------------------------------------------------------------
# 平台→负责人邮箱分发配置
# ---------------------------------------------------------------------------

RECIPIENT_CFG_PATH = ROOT / "config" / "platform_recipients.json"


def load_recipient_map():
    """加载平台→负责人邮箱分发配置。

    环境变量优先级高于配置文件：
      COMMENT_RECIPIENT_<PLATFORM>=邮箱（disabled 表示停用）
      COMMENT_OWNER_<PLATFORM>=负责人名称
      COMMENT_RECIPIENTS_JSON={"bilibili":{"email":"...","owner":"..."}}

    返回: (recipient_map, fallback_email)
        recipient_map: {platform: {owner, email, content_type, target_metrics}}
        fallback_email: 平台未映射时的兜底收件人
    """
    cfg = {}
    if RECIPIENT_CFG_PATH.exists():
        try:
            cfg = json.loads(RECIPIENT_CFG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = {}
    rmap = {
        platform: dict(details)
        for platform, details in (cfg.get("recipients", {}) or {}).items()
        if isinstance(details, dict)
    }

    json_override = os.environ.get("COMMENT_RECIPIENTS_JSON", "").strip()
    if json_override:
        try:
            decoded = json.loads(json_override)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"COMMENT_RECIPIENTS_JSON 格式错误: {exc}") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("COMMENT_RECIPIENTS_JSON 必须是 JSON 对象")
        for platform, details in decoded.items():
            if isinstance(details, str):
                details = {"email": details}
            if not isinstance(details, dict):
                raise RuntimeError(f"COMMENT_RECIPIENTS_JSON.{platform} 必须是对象或邮箱字符串")
            rmap.setdefault(platform, {}).update(details)

    platforms = set(PLATFORM_LABEL) | set(rmap)
    for platform in platforms:
        suffix = re.sub(r"[^A-Z0-9]+", "_", platform.upper())
        email = os.environ.get(f"COMMENT_RECIPIENT_{suffix}", "").strip()
        owner = os.environ.get(f"COMMENT_OWNER_{suffix}", "").strip()
        if email.lower() in {"disabled", "none", "off"}:
            rmap.pop(platform, None)
            continue
        if email:
            rmap.setdefault(platform, {})["email"] = email
        if owner:
            rmap.setdefault(platform, {})["owner"] = owner

    fallback = (os.environ.get("COMMENT_FALLBACK_EMAIL", "").strip()
                or cfg.get("fallback_email") or DEFAULT_RECIPIENT)
    return rmap, fallback


def resolve_recipient(platform, recipient_map):
    """返回 (email, owner, mapped)。mapped=False 表示平台未配置负责人。"""
    rec = recipient_map.get(platform)
    if rec and rec.get("email"):
        return rec["email"], rec.get("owner", ""), True
    return None, "", False


# ---------------------------------------------------------------------------
# 邮件提醒文件：供 send_comment_alerts.py 通过 SMTP 发送
# ---------------------------------------------------------------------------

ALERT_PATH = DATA_DIR / "comment_alert.json"


def write_alerts(new_items):
    """按收件人分组生成提醒文件 comment_alert.json。

    结构：payload["emails"] = [ {to, owner, subject, body, ...}, ... ]
    每个元素对应一封待发送邮件；未映射平台的 new_items 归入 payload["unmapped"]，
    不自动发送（避免发错人）。返回 emails 列表（空表示无需发送）。
    """
    if not new_items:
        return [], 0
    recipient_map, _fallback = load_recipient_map()

    groups = {}      # email -> {owner, platforms:set, items:[]}
    unmapped = []
    for item in new_items:
        platform = item.get("platform", "")
        email, owner, mapped = resolve_recipient(platform, recipient_map)
        if not mapped:
            unmapped.append(item)
            continue
        g = groups.setdefault(email, {"owner": owner,
                                      "platforms": set(), "items": []})
        g["platforms"].add(item.get("platform_label", "") or platform)
        g["items"].append(item)

    def split_batches(items, maximum):
        batches, current, current_size = [], [], 0
        expanded = []
        for source in items:
            comments = source.get("comments") or []
            if comments:
                for start in range(0, len(comments), maximum):
                    part = dict(source)
                    part["comments"] = comments[start:start + maximum]
                    expanded.append((part, len(part["comments"])))
            else:
                expanded.append((source, 1))
        for item, size in expanded:
            if current and current_size + size > maximum:
                batches.append(current)
                current, current_size = [], 0
            current.append(item)
            current_size += size
        if current:
            batches.append(current)
        return batches

    max_events = max(1, int(os.environ.get("COMMENT_EMAIL_MAX_EVENTS", "100")))
    emails = []
    for email, g in groups.items():
        batches = split_batches(g["items"], max_events)
        for index, batch in enumerate(batches, start=1):
            subject, body = build_mail(batch)
            if len(batches) > 1:
                subject += f"（{index}/{len(batches)}）"
            emails.append({
                "to": email,
                "owner": g["owner"],
                "subject": subject,
                "body": body,
                "body_format": "PLAIN",
                "platforms": sorted(g["platforms"]),
                "new_items": batch,
                "batch_index": index,
                "batch_total": len(batches),
            })

    now = datetime.now(CN_TZ).isoformat()
    for email in emails:
        email["id"] = str(uuid.uuid4())
        email["created_at"] = now

    # 保留上一轮尚未发送的邮件，SMTP 临时失败时由下个小时继续重试。
    previous = load_json(ALERT_PATH) or {}
    pending = previous.get("emails", []) if isinstance(previous, dict) else []
    previous_unmapped = previous.get("unmapped", []) if isinstance(previous, dict) else []
    payload = {
        "emails": [item for item in pending if isinstance(item, dict)] + emails,
        "unmapped": ([item for item in previous_unmapped if isinstance(item, dict)]
                     + unmapped),
        "generated_at": now,
    }
    # 兼容旧结构：仅一封邮件时同时保留顶层 to/subject/body
    if len(payload["emails"]) == 1:
        e0 = payload["emails"][0]
        payload["to"] = e0["to"]
        payload["subject"] = e0["subject"]
        payload["body"] = e0["body"]
        payload["body_format"] = "PLAIN"

    atomic_write_json(ALERT_PATH, payload, pretty=True)
    return emails, len(unmapped)


def build_mail(new_items):
    """生成邮件主题与正文。"""
    total = sum(len(i.get("comments", [])) or i.get("added_count", 0)
                for i in new_items)
    platforms = "、".join(sorted({i.get("platform_label", "")
                                  for i in new_items if i.get("platform_label")}))
    subject = f"[评论提醒] {platforms} 新增 {total} 条评论"
    return subject, build_email_text(new_items)


def main():
    ap = argparse.ArgumentParser(description="评论监控与邮件提醒")
    ap.add_argument("--dry-run", action="store_true",
                    help="只检查并打印结果，不写入提醒文件、不调用API（仅本地对比）")
    ap.add_argument("--limit", type=int, default=0,
                    help="每个账号检查最近 N 条内容；0 表示全部（默认 0）")
    ap.add_argument("--max-pages", type=int, default=200,
                    help="每条内容及每个回复线程最多翻页数；超限视为失败（默认 200）")
    ap.add_argument("--no-replies", action="store_true",
                    help="完全关闭官方回复检测")
    ap.add_argument("--no-discovery", action="store_true",
                    help="不在评论任务中检查各账号最新一页内容")
    ap.add_argument("--platform", action="append",
                    help="只检查指定平台或 platform:account_name（可重复），如 --platform douyin:望获OS")
    ap.add_argument("--recipient", default=DEFAULT_RECIPIENT, help="收件人邮箱")
    ap.add_argument("--max-age-days", type=int, default=0,
                    help="只看最近 N 天内发布的内容；0 表示全部（默认 0）")
    ap.add_argument("--fresh-days", type=int, default=7,
                    help="新内容分层天数（默认 7）")
    ap.add_argument("--recent-days", type=int, default=30,
                    help="近期内容分层天数（默认 30）")
    ap.add_argument("--fresh-hours", type=int, default=2,
                    help="新内容轮询间隔小时（默认 2）")
    ap.add_argument("--recent-hours", type=int, default=6,
                    help="近期内容轮询间隔小时（默认 6）")
    ap.add_argument("--older-hours", type=int, default=24,
                    help="较旧内容轮询间隔小时（默认 24）")
    ap.add_argument("--discovery-hours", type=int, default=2,
                    help="新作品发现间隔小时（默认 2）")
    ap.add_argument("--no-tiered-polling", action="store_false",
                    dest="tiered_polling", default=True,
                    help="关闭按内容年龄分层轮询")
    ap.add_argument("--no-api", action="store_true",
                    help="不调用 平台评论数据源，仅做评论数对比（无正文）")
    args = ap.parse_args()

    load_dotenv()
    now = datetime.now(CN_TZ)
    state = load_state()
    timeline = timeline_store.load_timeline(now=now)
    contents = build_content_list(max_age_days=args.max_age_days)
    if not args.dry_run:
        from providers.history import retire_replaced_comment_contents
        contents = retire_replaced_comment_contents(contents, state, timeline, load_json(VIDEO_DATA) or {})
    contents = merge_cached_discoveries(
        contents, state, args.max_age_days, now=now)
    print(f"内容清单：{len(contents)} 条（来自两个大屏数据）")

    # 按平台统计
    from collections import Counter
    by_platform = Counter(c["platform"] for c in contents)
    for p, n in by_platform.most_common():
        print(f"  - {PLATFORM_LABEL.get(p, p)}: {n} 条")

    client = None
    if not args.dry_run and not args.no_api:
        client = ProviderRegistry()

    discovery_errors = []
    if client and discovery_is_due(state, args, now=now):
        state["last_discovery_attempt_at"] = now.isoformat()
        try:
            contents, additions, discovery_errors = discover_latest_contents(client, contents, args.platform)
            cache_discoveries(state, additions)
            print(f"新内容发现：新增 {len(additions)} 条内容")
        except ApiBudgetExceeded as exc:
            discovery_errors.append(str(exc))
    elif client and not args.no_discovery:
        print(f"新内容发现：未到 {args.discovery_hours} 小时周期，本轮跳过")

    new_items, errors, state = check_comments(
        client, contents, args, state=state, timeline=timeline,
        official_identities=load_official_identities(), now=now)
    errors = discovery_errors + errors
    total_new = sum(len(i.get('comments', [])) or i.get('added_count', 0)
                    for i in new_items)
    is_baseline = not state.get("baseline_done", False)
    print(f"\n本轮共发现新增评论：{total_new} 条")

    def save_runtime():
        usage = client.usage_snapshot() if client else {"used": 0, "limit": 0, "remaining": 0, "source": "platform_direct"}
        timeline_store.save_timeline(
            timeline, api_usage=usage, last_scan=state.get("last_scan"))
        save_state(state)

    if errors:
        print("\n以下内容检查失败：", file=sys.stderr)
        for e in errors:
            print(f"  [warn] {e}", file=sys.stderr)

    # 首次运行：建立基线，只记录现有评论，不发提醒。
    # dry-run 必须完全只读，不能悄悄改变生产基线。
    if is_baseline:
        if args.dry_run:
            print("\n[dry-run] 当前尚未建立基线，本次不会写入状态文件。")
            return
        state["baseline_done"] = True
        state["baseline_at"] = datetime.now(CN_TZ).isoformat()
        save_runtime()
        print("\n[首次运行] 已记录现有评论作为基线，"
              "从下一次运行开始检测新评论并邮件提醒。")
        print(f"  基线记录内容数：{len(state.get('seen_comments', {}))} 条")
        if errors:
            raise SystemExit(2)
        return

    if new_items:
        print(f"\n=== 新增评论明细（{len(new_items)} 条内容）===")
        for item in new_items:
            print(f"[{item['platform_label']}] {item.get('title', '')[:30]}")
            if item.get("comments"):
                for c in item["comments"]:
                    print(f"  · {c.get('user','')}: {c.get('content','')[:50]}")
            else:
                print(f"  （新增 {item.get('added_count', 0)} 条评论）")

        if args.dry_run:
            print("\n[dry-run] 不发送邮件，以下是要发送的内容：")
            subject, body = build_mail(new_items)
            print("主题：", subject)
            print(body)
        else:
            # 时间线先落盘；待发队列成功后再推进评论状态。
            timeline_store.save_timeline(
                timeline, api_usage=client.usage_snapshot() if client else {"used": 0, "limit": 0, "remaining": 0, "source": "platform_direct"},
                last_scan=state.get("last_scan"))
            emails, unmapped_count = write_alerts(new_items)
            save_state(state)
            if emails:
                print(f"\n已生成提醒文件：{ALERT_PATH}")
                for e in emails:
                    print(f"  发送给 {e['owner']}({e['to']})：{e['subject']}")
                if unmapped_count:
                    print(f"  （另有 {unmapped_count} 条内容来自未配置负责人平台，不自动发送）")
            else:
                # 有新增但全部为未映射平台，不发邮件
                print(f"\n有新增评论，但这些平台未配置负责人收件人，不自动发送。")
                print(f"  未映射平台数量：{unmapped_count}")
    else:
        if not args.dry_run:
            save_runtime()
        # 不删除既有待发队列；发送失败的邮件必须由发送器继续重试。
        print("\n本轮没有新评论，无需提醒。")

    if errors:
        # 已完成的平台状态和待发邮件已经可靠保存；非零退出用于调度监控
        # 明确标记本轮并非完整成功。
        raise SystemExit(2)


if __name__ == "__main__":
    main()
