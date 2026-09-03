#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
评论监控与邮件提醒
==================
定时检查所有平台内容的新评论，发现新评论时发送邮件提醒。

覆盖平台：
  - 可抓评论正文：抖音、B站、小红书、视频号（通过 TikHub 评论接口）
  - 仅评论数检测：CSDN、知乎、今日头条、搜狐、百家号、公众号等（无公开评论正文接口）

工作方式（尽量简单）：
  1. 从大屏数据 JSON（data/dashboard_data.json + data/article_dashboard_data.json）
     读取各账号的内容清单（作品/文章 + 平台 + 内容ID）。
  2. 每小时读取四个明细平台的最新一页作品，补充每日快照后新发布的内容。
  3. 可抓正文的平台：默认检查全部内容，完整分页一级评论与二级回复，
     与上次已见评论ID对比，找出新增评论。
  4. 仅计数的平台：对比每日大屏快照中的评论数字，增长即提醒。
  5. 有新增评论时，写入待发邮件队列，由 send_comment_alerts.py 发送。
  6. 状态保存在 data/comment_state.json，避免重复提醒。

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

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
VIDEO_DATA = DATA_DIR / "dashboard_data.json"
ARTICLE_DATA = DATA_DIR / "article_dashboard_data.json"
STATE_PATH = DATA_DIR / "comment_state.json"
VIDEO_ACCOUNTS = ROOT / "config" / "accounts.json"
ARTICLE_ACCOUNTS = ROOT / "config" / "article_accounts.json"
BASE_URL = "https://api.tikhub.io"
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

# 可通过 TikHub 评论接口抓正文的平台 -> 接口配置
#   adapter: 平台标识
#   kind: 内容ID类型 (用于构造请求)
#   type: 内容类型标签
COMMENT_API_PLATFORMS = {
    "douyin": {
        "path": "/api/v1/douyin/app/v3/fetch_video_comments",
        "method": "get",
        "params": lambda item: {"aweme_id": item["content_id"], "cursor": 0, "count": 20},
        "type": "视频",
    },
    "bilibili": {
        "path": "/api/v1/bilibili/app/fetch_video_comments",
        "method": "get",
        "params": lambda item: {"bv_id": item["content_id"], "mode": 3, "next_offset": 1, "ps": 20},
        "type": "视频",
    },
    "xiaohongshu": {
        "path": "/api/v1/xiaohongshu/app_v2/get_note_comments",
        "method": "get",
        "params": lambda item: {"note_id": item["content_id"],
                                "cursor": "", "index": 0,
                                "pageArea": "UNFOLDED", "sort_strategy": "latest_v2"},
        "type": "笔记",
    },
    "wechat_channels": {
        "path": "/api/v1/wechat_channels/v2/fetch_video_comments",
        "method": "post",
        "params": lambda item: {"object_id": item["content_id"], "last_buffer": "",
                                "comment_id": "", "raw": False},
        "type": "视频",
    },
}


def load_dotenv():
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


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


class TikHubClient:
    """极简 TikHub 客户端，复用现有采集脚本的调用方式。"""

    def __init__(self, api_key: str, min_interval: float = 0.6):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        })
        self.min_interval = min_interval
        self.base_url = os.environ.get("TIKHUB_BASE_URL", BASE_URL).rstrip("/")
        self._last_call = 0.0
        self.call_count = 0

    def _throttle(self):
        wait = self.min_interval - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def request(self, method, path, params=None, payload=None, timeout=45, retries=3):
        url = f"{self.base_url}{path}"
        last_error = "未知错误"
        for attempt in range(1, retries + 1):
            self._throttle()
            self.call_count += 1
            try:
                resp = self.session.request(
                    method, url, params=params, json=payload, timeout=timeout)
                if resp.status_code == 200:
                    return resp.json()
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code not in {408, 429, 500, 502, 503, 504}:
                    break
            except requests.RequestException as exc:
                last_error = str(exc)
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(last_error)

    def get(self, path, **kwargs):
        kwargs.pop("tag", None)
        return self.request("GET", path, params=kwargs.pop("params", None), **kwargs)

    def post(self, path, **kwargs):
        kwargs.pop("tag", None)
        return self.request("POST", path, params=kwargs.pop("params", None),
                            payload=kwargs.pop("payload", None), **kwargs)


def unwrap_data(obj):
    """解开 TikHub 外层包装，找到真正的 data 节点。"""
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
        })

    # 过滤：无 ID、时间过早、无 URL（评论需要可访问的内容）
    cutoff = (datetime.now(CN_TZ) - timedelta(days=max_age_days)
              if max_age_days > 0 else None)
    filtered = []
    for c in contents:
        if not c["content_id"] or c["content_id"] in ("None", "0"):
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


def discover_latest_contents(client, existing):
    """每小时只拉各账号第一页作品，发现每日大屏刷新后的新内容。"""
    known = {(item["platform"], item["content_id"]) for item in existing}
    additions, errors = [], []
    video_snapshot = load_json(VIDEO_DATA) or {}
    snapshot_accounts = {
        (item.get("platform"), item.get("account_name")): item
        for item in video_snapshot.get("accounts", [])
    }
    configs = []
    for path in (VIDEO_ACCOUNTS, ARTICLE_ACCOUNTS):
        payload = load_json(path) or {}
        configs.extend(payload.get("accounts", []))

    for account in configs:
        platform = account.get("platform")
        if platform not in COMMENT_API_PLATFORMS:
            continue
        try:
            rows = []
            if platform == "bilibili":
                uid = str(account.get("platform_uid") or "")
                response = client.request(
                    "GET", "/api/v1/bilibili/app/fetch_user_videos",
                    params={"user_id": uid, "post_filter": "archive", "page": 1, "ps": 50})
                data = _deepest_data(response)
                for raw in dig(data, "item", "items", "list.vlist", "vlist", default=[]) or []:
                    content_id = str(dig(raw, "bvid", "param", "aid", default=""))
                    if not content_id:
                        continue
                    rows.append(_account_record(
                        account, content_id,
                        aid=dig(raw, "aid", "param", default=""),
                        title=dig(raw, "title", "name", default=""),
                        url=f"https://www.bilibili.com/video/{content_id}",
                        published_at=epoch_to_iso(dig(raw, "created", "pubdate")),
                        comment_count=dig(raw, "stat.reply", "reply")))

            elif platform == "douyin":
                # 复用主采集器的精确名称搜索回退，避免公开抖音号资料接口
                # 单点 400 导致新内容发现中断。
                from fetch_data import DouyinAdapter
                adapter = DouyinAdapter(client)
                info = adapter.resolve_user(account)
                videos = adapter.fetch_videos(info["sec_user_id"], max_pages=1)
                for raw in videos:
                    content_id = str(raw.get("video_id") or "")
                    if not content_id:
                        continue
                    rows.append(_account_record(
                        account, content_id,
                        title=raw.get("title") or "",
                        url=raw.get("url") or f"https://www.douyin.com/video/{content_id}",
                        published_at=raw.get("published_at"),
                        comment_count=dig(raw, "stats.comment")))

            elif platform == "wechat_channels":
                cached = snapshot_accounts.get((platform, account.get("account_name")), {})
                username = str(cached.get("platform_uid") or account.get("platform_uid") or "")
                if username.startswith("sph"):
                    resolved = client.request(
                        "POST", "/api/v1/wechat_channels/v2/fetch_channel_id_to_username",
                        payload={"channel_id": username, "raw": False})
                    username = str(dig(_deepest_data(resolved), "username", "finder_username") or "")
                if not username:
                    raise RuntimeError("无法解析视频号 username")
                response = client.request(
                    "POST", "/api/v1/wechat_channels/v2/fetch_user_videos",
                    payload={"username": username, "last_buffer": "", "raw": False})
                data = _deepest_data(response)
                for raw in dig(data, "videos", "items", "list", "objects", default=[]) or []:
                    content_id = str(dig(raw, "object_id", "objectId", "id", default=""))
                    if not content_id:
                        continue
                    rows.append(_account_record(
                        account, content_id,
                        title=dig(raw, "title", "description", "desc", default=""),
                        url=dig(raw, "share_url", "shareUrl", "url", default=""),
                        published_at=epoch_to_iso(
                            dig(raw, "create_time", "createtime", "createTime")),
                        comment_count=dig(raw, "comment_count", "commentCount")))

            elif platform == "xiaohongshu":
                user_id = str(account.get("platform_uid") or "")
                response = client.request(
                    "GET", "/api/v1/xiaohongshu/app_v2/get_user_posted_notes",
                    params={"user_id": user_id, "cursor": ""})
                data = _deepest_data(response)
                for raw in data.get("notes") or []:
                    content_id = str(raw.get("id") or "")
                    if not content_id:
                        continue
                    rows.append(_account_record(
                        account, content_id,
                        title=raw.get("title") or raw.get("display_title") or "无标题笔记",
                        url=f"https://www.xiaohongshu.com/explore/{content_id}",
                        published_at=epoch_to_iso(raw.get("create_time")),
                        comment_count=raw.get("comments_count")))

            for row in rows:
                identity = (row["platform"], row["content_id"])
                if identity in known:
                    continue
                known.add(identity)
                additions.append(row)
        except Exception as exc:
            errors.append(
                f"{PLATFORM_LABEL.get(platform, platform)} {account.get('account_name', '')} "
                f"新内容发现失败: {compact_error(exc, 120)}")
    return existing + additions, additions, errors


# ---------------------------------------------------------------------------
# 评论正文解析：各平台从响应中提取评论列表
# ---------------------------------------------------------------------------

def parse_douyin_comments(data):
    data = unwrap_data(data)
    comments = dig(data, "comments", "data.comments", default=[]) or []
    out = []
    for c in comments:
        cid = str(dig(c, "cid", "comment_id", "id", default=""))
        if not cid:
            continue
        out.append({
            "comment_id": cid,
            "content": dig(c, "text", "content", default="") or "",
            "user": dig(c, "user.nickname", "nickname", default="") or "",
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
        out.append({
            "comment_id": cid,
            "content": dig(c, "content", default="") or "",
            "user": dig(c, "nickname", "username", default="") or "",
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


def check_comments(client, contents, args):
    """执行一轮评论检查，返回新评论列表和状态更新。"""
    state = load_state()
    seen = state.get("seen_comments", {})      # content_id -> {comment_id: 1}
    counts = state.get("content_counts", {})   # content_id -> last_comment_count
    full_scan_baselines = set(state.get("full_scan_baselines", []))
    new_items = []
    errors = []
    scan_totals = {
        "detail_contents": 0, "root_pages": 0,
        "reply_pages": 0, "public_comments": 0,
    }

    # 按账号分组，每个账号只看最近 N 条内容（评论通常集中在最新内容）
    by_account = {}
    for c in contents:
        by_account.setdefault(c["account_key"], []).append(c)
    for key in by_account:
        by_account[key].sort(key=lambda x: x.get("published_at") or "", reverse=True)

    platform_filter = set(args.platform or [])
    total_new = 0

    for account_key, items in by_account.items():
        platform = items[0]["platform"]
        label = items[0]["platform_label"] or PLATFORM_LABEL.get(platform, platform)
        account_name = items[0]["account_name"]
        if platform_filter and platform not in platform_filter \
                and label not in platform_filter and account_key not in platform_filter:
            continue
        recent = items if args.limit <= 0 else items[: args.limit]
        scope_text = "全部" if args.limit <= 0 else "最近"
        print(f">>> {label} / {account_name}：检查{scope_text} {len(recent)} 条内容")

        for item in recent:
            cid = item["content_id"]
            key_of_cid = f"{platform}:{cid}"

            # ---- 可抓正文的平台：拉评论列表，对比已见评论ID ----
            if platform in COMMENT_API_PLATFORMS and client:
                try:
                    all_comments, page_stats = fetch_all_comments(
                        client, platform, item,
                        max_pages=args.max_pages,
                        include_replies=not args.no_replies)
                    scan_totals["detail_contents"] += 1
                    scan_totals["root_pages"] += page_stats["root_pages"]
                    scan_totals["reply_pages"] += page_stats["reply_pages"]
                    scan_totals["public_comments"] += len(all_comments)
                    all_ids = {comment["comment_id"] for comment in all_comments}
                    previously_seen = set(seen.get(key_of_cid, []))

                    # 旧版本只保存首屏。每条内容首次升级为完整分页时建立一次
                    # 全量基线，避免把多年历史评论误报为刚刚新增。
                    if key_of_cid not in full_scan_baselines:
                        seen[key_of_cid] = sorted(previously_seen | all_ids)
                        full_scan_baselines.add(key_of_cid)
                        if not state.get("baseline_done", False):
                            if all_comments:
                                print(
                                    f"    启动基线：{len(all_comments)} 条公开评论/回复，"
                                    f"一级 {page_stats['root_pages']} 页，"
                                    f"回复 {page_stats['reply_pages']} 页")
                            continue

                        # 服务基线完成后才发现的新内容：只提醒明确发布于
                        # monitor_started_at 之后的评论，其余作为存量基线。
                        new_comments = [
                            comment for comment in all_comments
                            if comment_created_after(
                                comment, state.get("monitor_started_at"))]
                        if all_comments and not new_comments:
                            print(
                                f"    新内容存量基线：{len(all_comments)} 条，"
                                "无启动时刻后的可靠时间戳评论")
                    else:
                        new_comments = [
                            comment for comment in all_comments
                            if comment["comment_id"] not in previously_seen]
                        seen[key_of_cid] = sorted(previously_seen | all_ids)
                    if new_comments:
                        entry = dict(item)
                        entry["comments"] = new_comments
                        entry["scan_pages"] = page_stats
                        entry["platform_label"] = label
                        new_items.append(entry)
                        total_new += len(new_comments)
                except Exception as e:
                    errors.append(f"{label} {cid}: {compact_error(e, 120)}")
            elif platform in COMMENT_API_PLATFORMS \
                    and not (getattr(args, "dry_run", False)
                             or getattr(args, "no_api", False)):
                errors.append(f"{label}: 未配置 TIKHUB_API_KEY，无法检查评论明细")
            # ---- 仅计数的平台：对比评论数增长 ----
            else:
                current = item.get("stats_comment")
                last = counts.get(key_of_cid)
                if current is not None and last is not None and current > last:
                    entry = dict(item)
                    entry["comments"] = []
                    entry["added_count"] = current - last
                    entry["platform_label"] = label
                    new_items.append(entry)
                    total_new += entry["added_count"]
                if current is not None:
                    counts[key_of_cid] = current

    state["seen_comments"] = {k: list(v) for k, v in seen.items()}
    state["content_counts"] = counts
    state["full_scan_baselines"] = sorted(full_scan_baselines)
    state["last_checked_at"] = datetime.now(CN_TZ).isoformat()
    state["last_scan"] = {
        **scan_totals,
        "errors": len(errors),
        "complete": not errors,
    }
    if total_new > 0:
        state["last_new_count"] = total_new
    return new_items, errors, state


REPLY_API_PLATFORMS = {
    "douyin": {
        "path": "/api/v1/douyin/app/v3/fetch_video_comment_replies",
        "method": "get",
    },
    "bilibili": {
        "path": "/api/v1/bilibili/app/fetch_reply_detail",
        "method": "get",
    },
    "xiaohongshu": {
        "path": "/api/v1/xiaohongshu/app_v2/get_note_sub_comments",
        "method": "get",
    },
    "wechat_channels": {
        "path": "/api/v1/wechat_channels/v2/fetch_video_comments",
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


def _request_comment_page(client, endpoint, params):
    if endpoint["method"] == "post":
        return client.request("POST", endpoint["path"], payload=params)
    return client.request("GET", endpoint["path"], params=params)


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


def _fetch_pages(client, platform, endpoint, initial_params, max_pages, parent_id=""):
    comments = []
    params = initial_params
    page_count = 0
    visited = set()
    while True:
        fingerprint = json.dumps(params, ensure_ascii=False, sort_keys=True)
        if fingerprint in visited:
            raise RuntimeError(f"{PLATFORM_LABEL.get(platform, platform)} 分页游标重复")
        visited.add(fingerprint)
        response = _request_comment_page(client, endpoint, params)
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


def fetch_all_comments(client, platform, item, max_pages=200, include_replies=True):
    """完整分页拉取一级评论，并按回复数继续完整拉取二级回复。"""
    roots, pages = _fetch_pages(
        client, platform, COMMENT_API_PLATFORMS[platform],
        _initial_root_request(platform, item), max_pages)
    comments = list(roots)
    reply_pages = 0
    if include_replies:
        for root in roots:
            if to_int(root.get("reply_count"), 0) <= 0:
                continue
            root_id = str(root["comment_id"])
            replies, used_pages = _fetch_pages(
                client, platform, REPLY_API_PLATFORMS[platform],
                _initial_reply_request(platform, item, root_id), max_pages,
                parent_id=root_id)
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
                    help="只采集一级评论，不继续采集二级回复")
    ap.add_argument("--no-discovery", action="store_true",
                    help="不在评论任务中检查各账号最新一页内容")
    ap.add_argument("--platform", action="append",
                    help="只检查指定平台（可重复），如 --platform bilibili --platform douyin")
    ap.add_argument("--recipient", default=DEFAULT_RECIPIENT, help="收件人邮箱")
    ap.add_argument("--max-age-days", type=int, default=0,
                    help="只看最近 N 天内发布的内容；0 表示全部（默认 0）")
    ap.add_argument("--no-api", action="store_true",
                    help="不调用 TikHub 评论接口，仅做评论数对比（无正文）")
    args = ap.parse_args()

    load_dotenv()
    contents = build_content_list(max_age_days=args.max_age_days)
    print(f"内容清单：{len(contents)} 条（来自两个大屏数据）")

    # 按平台统计
    from collections import Counter
    by_platform = Counter(c["platform"] for c in contents)
    for p, n in by_platform.most_common():
        print(f"  - {PLATFORM_LABEL.get(p, p)}: {n} 条")

    client = None
    if not args.dry_run and not args.no_api:
        api_key = os.environ.get("TIKHUB_API_KEY", "").strip()
        if not api_key:
            print("[提示] 未设置 TIKHUB_API_KEY，仅做评论数对比（无法抓正文）",
                  file=sys.stderr)
        else:
            client = TikHubClient(api_key)

    discovery_errors = []
    if client and not args.no_discovery:
        contents, additions, discovery_errors = discover_latest_contents(client, contents)
        print(f"小时级新内容发现：新增 {len(additions)} 条内容")

    new_items, errors, state = check_comments(client, contents, args)
    errors = discovery_errors + errors
    total_new = sum(len(i.get('comments', [])) or i.get('added_count', 0)
                    for i in new_items)
    is_baseline = not state.get("baseline_done", False)
    print(f"\n本轮共发现新增评论：{total_new} 条")

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
        save_state(state)
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
            # 先可靠写入待发队列，再推进评论游标，避免写队列失败导致提醒永久丢失。
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
            save_state(state)
        # 不删除既有待发队列；发送失败的邮件必须由发送器继续重试。
        print("\n本轮没有新评论，无需提醒。")

    if errors:
        # 已完成的平台状态和待发邮件已经可靠保存；非零退出用于调度监控
        # 明确标记本轮并非完整成功。
        raise SystemExit(2)


if __name__ == "__main__":
    main()
