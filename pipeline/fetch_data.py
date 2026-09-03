#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TikHub 视频数据采集器
=====================
从 TikHub API (https://api.tikhub.io) 拉取抖音 / B站 / 微信视频号账号的全部作品数据,
归一化为统一 schema 后写入 data/dashboard_data.json 供前端大屏使用。

用法:
    export TIKHUB_API_KEY="your_token"        # 或写入 .env
    python3 pipeline/fetch_data.py                    # 全量采集
    python3 pipeline/fetch_data.py --mock             # 生成演示数据(无需API key)
    python3 pipeline/fetch_data.py --no-enrich-bili   # B站不逐条补点赞/投币(省额度)
    python3 pipeline/fetch_data.py --debug            # 保存原始响应到 data/debug/

注意: 视频号无法按名称搜索, 必须在 config/accounts.json 中填入 sph 开头的短号。
"""

import argparse
import json
import re
import os
import random
import sys
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

from snapshot_utils import (
    atomic_write_json,
    atomic_write_text,
    finalize_snapshot,
    is_suspicious_drop,
)

try:
    import requests
except ImportError:
    sys.exit("缺少依赖: pip3 install requests")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "accounts.json"
OUT_PATH = ROOT / "data" / "dashboard_data.json"
DEBUG_DIR = ROOT / "data" / "debug"
BASE_URL = "https://api.tikhub.io"
CN_TZ = timezone(timedelta(hours=8))


def load_dotenv():
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def norm_name(s: str) -> str:
    """宽松比较账号名: 去空白、统一大小写与全半角。"""
    return unicodedata.normalize("NFKC", s or "").replace(" ", "").lower()


def to_int(v, default=None):
    try:
        return int(str(v).replace(",", "").replace("+", ""))
    except (TypeError, ValueError):
        return default


def epoch_to_iso(ts):
    ts = to_int(ts)
    if not ts:
        return None
    if ts > 10_000_000_000:
        ts //= 1000
    return datetime.fromtimestamp(ts, tz=CN_TZ).isoformat()


def duration_seconds(value):
    """兼容秒数、毫秒数以及 01:23 / 1:02:03 时长文本。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number // 1000 if number > 100_000 else number
    text = str(value).strip()
    if ":" in text:
        try:
            parts = [int(part) for part in text.split(":")]
        except ValueError:
            return None
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return to_int(text)


class TikHubClient:
    def __init__(self, api_key: str, debug: bool = False, min_interval: float = 0.6):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        })
        self.debug = debug
        self.min_interval = min_interval
        self._last_call = 0.0
        self.call_count = 0

    def _throttle(self):
        wait = self.min_interval - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _request(self, method: str, path: str, *, params=None, payload=None,
                 timeout=35, retries=3, tag=""):
        url = f"{BASE_URL}{path}"
        last_err = None
        for attempt in range(1, retries + 1):
            self._throttle()
            self.call_count += 1
            try:
                resp = self.session.request(method, url, params=params,
                                            json=payload, timeout=timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    if self.debug:
                        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                        fn = DEBUG_DIR / f"{tag or 'raw'}_{int(time.time() * 1000)}.json"
                        fn.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    return data
                last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code in (401, 403):
                    raise RuntimeError(f"API Key 无效或额度不足 ({resp.status_code})")
            except requests.RequestException as e:
                last_err = str(e)
            time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"请求失败 {path}: {last_err}")

    def get(self, path, **kw):
        return self._request("GET", path, **kw)

    def post(self, path, **kw):
        return self._request("POST", path, **kw)


def dig(obj, *paths, default=None):
    """按多个候选路径取值, 返回第一个非 None 的。"""
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
    """解开 TikHub 外层以及上游平台常见的 code/message/data 包装。"""
    cur = obj
    for _ in range(5):
        if not isinstance(cur, dict) or not isinstance(cur.get("data"), dict):
            break
        # TikHub 外层一定带 request_id/router；B站上游常见 code/message/ttl/data。
        if "request_id" in cur or "router" in cur or set(cur).issubset({"code", "message", "ttl", "data"}):
            cur = cur["data"]
        else:
            break
    return cur


def parse_json_object(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def compact_error(exc, limit=500):
    text = re.sub(r"\s+", " ", str(exc)).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def first_nonempty(*values, default=""):
    """返回第一个非 None 且非空字符串/容器的值。"""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        return value
    return default


class DouyinAdapter:
    platform = "douyin"

    def __init__(self, client: TikHubClient):
        self.c = client

    @staticmethod
    def _user_candidates(item):
        """兼容旧版 user_info 与新版 dynamic_patch.raw_data / V2 精简结构。"""
        candidates = []
        if not isinstance(item, dict):
            return candidates
        for direct in (item.get("user_info"), item.get("user"), item):
            if isinstance(direct, dict):
                candidates.append(direct)
        for container in (item, item.get("dynamic_patch"), item.get("user")):
            if not isinstance(container, dict):
                continue
            raw = parse_json_object(container.get("raw_data"))
            if raw:
                info = raw.get("user_info", raw)
                if isinstance(info, dict):
                    candidates.insert(0, info)
        return candidates

    def resolve_user(self, account: dict) -> dict:
        """定位账号 -> {sec_user_id, nickname, followers, unique_id}。"""
        uid = (account.get("platform_uid") or "").strip()
        profile_error = None
        if uid:
            # platform_uid 可直接填写 sec_uid，也可填写公开抖音号 unique_id。
            if uid.startswith("MS4wLjAB"):
                return {"sec_user_id": uid, "nickname": account["account_name"],
                        "followers": None, "unique_id": ""}
            try:
                r = self.c.get("/api/v1/douyin/web/handler_user_profile_v2",
                               params={"unique_id": uid}, tag="douyin_profile")
                u = unwrap_data(r)
                u = dig(u, "user_info", "user", default=u) or {}
                sec = dig(u, "sec_uid", "secUid", "sec_user_id")
                if sec:
                    return {"sec_user_id": sec,
                            "nickname": dig(u, "nickname", "nick_name", default=account["account_name"]),
                            "followers": to_int(dig(u, "follower_count", "mplatform_followers_count", "fans_cnt")),
                            "unique_id": dig(u, "unique_id", default=uid)}
            except Exception as exc:
                profile_error = exc

        # V2 给出稳定的 sec_uid/nickname/fans；如果无精确命中，再退回完整版搜索。
        searches = [
            ("/api/v1/douyin/search/fetch_user_search_v2", {"keyword": account["account_name"], "cursor": 0}),
            ("/api/v1/douyin/search/fetch_user_search", {"keyword": account["account_name"], "cursor": 0}),
        ]
        best = None
        target = norm_name(account["account_name"])
        for path, payload in searches:
            r = self.c.post(path, payload=payload, tag="douyin_user_search")
            data = unwrap_data(r)
            users = dig(data, "user_list", "users", "data.user_list", default=[]) or []
            for item in users:
                for info in self._user_candidates(item):
                    nick = dig(info, "nickname", "nick_name", default="") or ""
                    sec = dig(info, "sec_uid", "secUid", "sec_user_id", "user_id")
                    if not sec:
                        continue
                    candidate = {
                        "sec_user_id": sec,
                        "nickname": nick or account["account_name"],
                        "followers": to_int(dig(info, "follower_count", "fans_cnt")),
                        "unique_id": dig(info, "unique_id", default="") or "",
                    }
                    if norm_name(nick) == target:
                        return candidate
                    if best is None:
                        best = candidate
            if best and path.endswith("_v2"):
                # 完整版 raw_data 有机会找到更精确的公开抖音号，继续一次。
                continue
        if not best:
            if profile_error is not None:
                raise RuntimeError(
                    f"抖音号资料接口和名称搜索均失败: {compact_error(profile_error, 180)}")
            raise RuntimeError(f"抖音搜索未找到账号: {account['account_name']}")
        return best

    def fetch_videos(self, sec_user_id: str, max_pages=200) -> list:
        videos, cursor, pages, seen_ids = [], "0", 0, set()

        def fetch_page(page_cursor, tag):
            try:
                return self.c.get(
                    "/api/v1/douyin/web/fetch_user_post_videos",
                    params={"sec_user_id": sec_user_id,
                            "max_cursor": page_cursor, "count": 20},
                    tag=tag)
            except Exception as web_error:
                # 部分账号的 Web 作品接口会稳定返回 400；App V3 使用同一
                # sec_uid，并提供 latest 排序和 normal/lite 双渠道。
                last_error = web_error
                for channel in ("normal", "lite"):
                    try:
                        return self.c.get(
                            "/api/v1/douyin/app/v3/fetch_user_post_videos",
                            params={"sec_user_id": sec_user_id,
                                    "max_cursor": page_cursor, "count": 20,
                                    "sort_type": 0, "channel": channel},
                            tag=f"{tag}_app_{channel}")
                    except Exception as exc:
                        last_error = exc
                raise RuntimeError(
                    f"抖音 Web/App 作品接口均失败: {compact_error(last_error, 180)}")

        while pages < max_pages:
            r = fetch_page(cursor, "douyin_user_videos")
            data = unwrap_data(r)
            items = dig(data, "aweme_list", "items", "list", default=[]) or []
            # 上游偶尔会在翻页时只返回 {status_code: 0}，不能误判为采集结束。
            # 对已明确还有下一页的请求重试；仍为空则报错，避免静默生成不完整数据。
            if not items and pages > 0:
                for retry_no in range(2):
                    time.sleep(1.2 * (retry_no + 1))
                    r = fetch_page(cursor, "douyin_user_videos_retry")
                    data = unwrap_data(r)
                    items = dig(data, "aweme_list", "items", "list", default=[]) or []
                    if items:
                        break
                if not items:
                    raise RuntimeError(f"抖音作品翻页返回空数据，cursor={cursor}，为避免漏数已中止该账号采集")
            for it in items:
                video_id = str(dig(it, "aweme_id", "id", default=""))
                if not video_id or video_id in seen_ids:
                    continue
                seen_ids.add(video_id)
                st = dig(it, "statistics", default={}) or {}
                videos.append({
                    "video_id": video_id,
                    "title": dig(it, "desc", "title", default=""),
                    "cover": dig(it, "video.cover.url_list.0", "video.origin_cover.url_list.0", default=""),
                    "url": dig(it, "share_info.share_url", "share_url", default="") or
                           f"https://www.douyin.com/video/{video_id}",
                    "published_at": epoch_to_iso(it.get("create_time")),
                    "duration": (to_int(dig(it, "video.duration", "duration")) or 0) // 1000 or None,
                    "stats": {
                        "play": to_int(st.get("play_count")),
                        "like": to_int(st.get("digg_count")),
                        "comment": to_int(st.get("comment_count")),
                        "reply": to_int(dig(st, "reply_count", default=None)),
                        "share": to_int(st.get("share_count")),
                        "download": to_int(st.get("download_count")),
                        "collect": to_int(st.get("collect_count")),
                    },
                })
            has_more = bool(dig(data, "has_more", "hasMore", default=0))
            next_cursor = str(dig(data, "max_cursor", "maxCursor", default="0"))
            pages += 1
            if not has_more or next_cursor in ("0", "None", "") or next_cursor == cursor:
                break
            cursor = next_cursor
        # 抖音网页端当前常把所有作品 play_count 统一返回 0，这代表未公开而非真实零播放。
        known_plays = [v["stats"].get("play") for v in videos]
        if videos and known_plays and all(v == 0 for v in known_plays):
            for video in videos:
                video["stats"]["play"] = None
        return videos


class BilibiliAdapter:
    platform = "bilibili"

    def __init__(self, client: TikHubClient, enrich=True, max_enrich=300):
        self.c = client
        self.enrich = enrich
        self.max_enrich = max_enrich

    def _profile(self, mid: str, fallback_name: str) -> dict:
        """优先 app 用户信息；部分新 UID 调 web profile 会返回 400。"""
        try:
            r = self.c.get("/api/v1/bilibili/app/fetch_user_info",
                           params={"user_id": mid}, tag="bili_profile")
            data = unwrap_data(r)
            card = dig(data, "card", default=data) or {}
            return {"mid": str(mid), "nickname": card.get("name", fallback_name),
                    "followers": to_int(card.get("fans"))}
        except Exception:
            r = self.c.get("/api/v1/bilibili/web/fetch_user_profile",
                           params={"uid": mid}, tag="bili_profile_web")
            data = unwrap_data(r)
            card = dig(data, "card", default=data) or {}
            return {"mid": str(mid), "nickname": card.get("name", fallback_name),
                    "followers": to_int(card.get("fans"))}

    def resolve_user(self, account: dict) -> dict:
        uid = (account.get("platform_uid") or "").strip()
        if uid:
            return self._profile(uid, account["account_name"])
        r = self.c.get("/api/v1/bilibili/app/fetch_search_by_type",
                       params={"keyword": account["account_name"], "search_type": "user",
                               "cursor": "", "page_size": 20},
                       tag="bili_user_search")
        data = unwrap_data(r)
        users = dig(data, "items", "result", "data.items", default=[]) or []
        best, target = None, norm_name(account["account_name"])
        for item in users:
            author = item.get("author") if isinstance(item, dict) else None
            info = author if isinstance(author, dict) else item
            name = dig(info, "title", "uname", "name", default="") or ""
            name = re.sub(r"<[^>]+>", "", name)
            mid = dig(item, "param", "mid", "user_id") or dig(info, "mid", "user_id")
            if not mid:
                continue
            candidate = {"mid": str(mid), "nickname": name or account["account_name"],
                         "followers": to_int(dig(info, "fans", "follower_count"))}
            if norm_name(name) == target:
                best = candidate
                break
            if best is None:
                best = candidate
        if not best:
            raise RuntimeError(f"B站搜索未找到账号: {account['account_name']}")
        # 搜索结果已有粉丝数，避免额外计费；缺失时再补 profile。
        if best.get("followers") is None:
            return self._profile(best["mid"], best["nickname"])
        return best

    def fetch_videos(self, mid: str, max_pages=100) -> list:
        """使用 app 投稿接口，兼容超长 UID；每页 50 条直到 count 全量取完。"""
        videos, page, total, seen_ids = [], 1, None, set()
        while page <= max_pages:
            r = self.c.get("/api/v1/bilibili/app/fetch_user_videos",
                           params={"user_id": mid, "post_filter": "archive", "page": page, "ps": 50},
                           tag="bili_user_videos")
            data = unwrap_data(r)
            items = dig(data, "item", "items", "list.vlist", "vlist", default=[]) or []
            if not items:
                break
            total = to_int(dig(data, "count", "page.count", "page.total"), default=total)
            for it in items:
                bvid = str(dig(it, "bvid", default=""))
                video_id = bvid or str(dig(it, "param", "aid", default=""))
                if not video_id or video_id in seen_ids:
                    continue
                seen_ids.add(video_id)
                cover = str(dig(it, "cover", "pic", default="") or "")
                if cover.startswith("//"):
                    cover = "https:" + cover
                elif cover.startswith("http:"):
                    cover = "https:" + cover[5:]
                videos.append({
                    "video_id": video_id,
                    "aid": str(dig(it, "param", "aid", default="") or ""),
                    "cid": str(dig(it, "first_cid", "cid", default="") or ""),
                    "title": it.get("title", ""),
                    "cover": cover,
                    "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                    "published_at": epoch_to_iso(dig(it, "ctime", "created", "pubdate")),
                    "duration": duration_seconds(it.get("duration")),
                    "source_author": dig(it, "author", "owner.name", "upper.name", default="") or "",
                    "source_author_id": str(dig(it, "mid", "owner.mid", "upper.mid", default="") or ""),
                    "stats": {
                        "play": to_int(it.get("play")),
                        "comment": to_int(dig(it, "comment", "reply")),
                        "reply": to_int(dig(it, "comment", "reply")),
                        "danmaku": to_int(dig(it, "danmaku", "video_review", "review")),
                        "collect": to_int(dig(it, "favorites", "favorite")),
                        "like": None, "share": None, "coin": None, "download": None,
                    },
                })
            if total is not None and len(videos) >= total:
                break
            page += 1
        if self.enrich:
            self._enrich(videos)
        return videos

    def _enrich(self, videos: list):
        """逐条拉视频详情，补 like/coin/share/danmaku/评论等指标。"""
        limit = min(len(videos), self.max_enrich)
        for i in range(limit):
            bvid = videos[i]["video_id"]
            if not bvid or not bvid.startswith("BV"):
                continue
            try:
                r = self.c.get("/api/v1/bilibili/web/fetch_one_video",
                               params={"bv_id": bvid}, tag="bili_video_detail")
                data = unwrap_data(r)
                st = dig(data, "stat", default={}) or {}
                owner = dig(data, "owner", default={}) or {}
                if isinstance(owner, dict):
                    videos[i]["source_author"] = owner.get("name") or videos[i].get("source_author", "")
                    videos[i]["source_author_id"] = str(owner.get("mid") or videos[i].get("source_author_id", ""))
                for k_src, k_dst in (("like", "like"), ("coin", "coin"),
                                     ("share", "share"), ("danmaku", "danmaku"),
                                     ("favorite", "collect"), ("reply", "comment"),
                                     ("view", "play")):
                    val = to_int(st.get(k_src))
                    if val is not None:
                        videos[i]["stats"][k_dst] = val
                videos[i]["stats"]["reply"] = to_int(st.get("reply"))
            except Exception as e:
                print(f"    [warn] B站详情补全失败 {bvid}: {compact_error(e, 180)}", file=sys.stderr)
            if (i + 1) % 20 == 0:
                print(f"    B站详情补全进度 {i + 1}/{limit}")


def wechat_title(value, video_id=""):
    """视频号 title 可能是字符串，也可能是 [{shortTitle: ...}]。"""
    if isinstance(value, str):
        title = value.strip()
    elif isinstance(value, list):
        title = ""
        for item in value:
            if isinstance(item, dict):
                title = str(item.get("shortTitle") or item.get("title") or "").strip()
                if title:
                    break
    elif isinstance(value, dict):
        title = str(value.get("shortTitle") or value.get("title") or "").strip()
    else:
        title = ""
    return title or (f"视频号作品 {video_id}" if video_id else "视频号作品")


class WeChatChannelsAdapter:
    platform = "wechat_channels"

    def __init__(self, client: TikHubClient):
        self.c = client

    def resolve_user(self, account: dict) -> dict:
        identifier = (account.get("platform_uid") or "").strip()
        if identifier.startswith("v2_") and identifier.endswith("@finder"):
            username = identifier
        elif identifier.startswith("sph"):
            r = self.c.post("/api/v1/wechat_channels/v2/fetch_channel_id_to_username",
                            payload={"channel_id": identifier, "raw": False}, timeout=45,
                            tag="wx_id_to_username")
            data = unwrap_data(r)
            username = dig(data, "username", "finder_username", "data.username")
            resolved_nickname = dig(data, "nickname", default="") or ""
            if not username:
                raise RuntimeError(f"视频号短号解析失败: {identifier}")
        else:
            raise RuntimeError(
                f"视频号 [{account['account_name']}] 需要在 config/accounts.json 填写 "
                "sph 开头短号或 v2_…@finder username；TikHub 当前没有按昵称搜索账号接口")
        prof = self.c.post("/api/v1/wechat_channels/v2/fetch_user_profile",
                           payload={"username": username, "raw": False},
                           timeout=45, tag="wx_profile")
        info = unwrap_data(prof) or {}
        api_nickname = dig(info, "nickname", "nickName", "profile.nickname", default="") or ""
        # 个别账号接口会把 sph 短号当昵称返回，展示时应使用配置中的正式账号名。
        nickname = api_nickname
        if not nickname or nickname.lower().startswith("sph"):
            nickname = account["account_name"]
        followers = to_int(dig(info, "follower_count", "fans_count", "profile.follower_count"))
        feeds_count = to_int(dig(info, "feeds_count", "profile.feeds_count"))
        if followers == 0 and feeds_count:
            followers = None  # 视频号公开接口常用 0 作为未返回粉丝数的占位值
        return {"username": username,
                "nickname": nickname,
                "followers": followers,
                "feeds_count": feeds_count}

    def fetch_videos(self, username: str, max_pages=200) -> list:
        videos, buf, pages, seen_ids = [], "", 0, set()
        while pages < max_pages:
            r = self.c.post("/api/v1/wechat_channels/v2/fetch_user_videos",
                            payload={"username": username, "last_buffer": buf, "raw": False},
                            timeout=45, tag="wx_user_videos")
            data = unwrap_data(r)
            items = dig(data, "videos", "items", "list", "objects", default=[]) or []
            for it in items:
                video_id = str(dig(it, "object_id", "objectId", "id", default=""))
                if not video_id or video_id in seen_ids:
                    continue
                seen_ids.add(video_id)
                media = dig(it, "media", default={}) or {}
                videos.append({
                    "video_id": video_id,
                    "title": wechat_title(dig(it, "title", "description", "desc", "objectDesc.description", default=""), video_id),
                    "cover": first_nonempty(
                        dig(it, "cover_img_url"), dig(it, "cover_url"), dig(it, "coverUrl"),
                        dig(it, "thumb_url"), dig(it, "media.cover_url"), dig(it, "media.coverUrl")),
                    # 视频号作品页不提供稳定公开 URL，保留 TikHub 返回的临时媒体链接供查看/下载。
                    "url": first_nonempty(
                        dig(it, "share_url"), dig(it, "shareUrl"), dig(it, "media.full_url"),
                        dig(it, "media.url"), dig(it, "url")),
                    "published_at": epoch_to_iso(dig(it, "create_time", "createtime", "createTime")),
                    "duration": to_int(dig(it, "duration", "media.duration")),
                    "stats": {
                        "play": to_int(dig(it, "read_count", "readCount", "play_count", "playCount")),
                        "like": to_int(dig(it, "like_count", "likeCount")),
                        "comment": to_int(dig(it, "comment_count", "commentCount")),
                        "reply": to_int(dig(it, "reply_count", "replyCount")),
                        "share": to_int(dig(it, "forward_count", "forwardCount", "share_count", "shareCount")),
                        "collect": to_int(dig(it, "fav_count", "favCount", "collect_count", "collectCount")),
                        "download": to_int(dig(it, "download_count", "downloadCount")),
                    },
                })
            next_buf = dig(data, "last_buffer", "lastBuffer", "next_buffer", default="") or ""
            pages += 1
            # 当前 TikHub 视频号精简响应中 up_continue 可能始终为 0，但 last_buffer 仍可取到下一页。
            # 因此以“本页有数据 + 游标变化”为准继续，直到真正返回空列表。
            if not items or not next_buf or next_buf == buf:
                break
            buf = next_buf
        # 当前接口的 read_count 常统一返回 0，属于未公开占位值而非真实播放量。
        plays = [v["stats"].get("play") for v in videos]
        if videos and plays and all(v == 0 for v in plays):
            for video in videos:
                video["stats"]["play"] = None
        return videos


ADAPTERS = {
    "douyin": DouyinAdapter,
    "bilibili": BilibiliAdapter,
    "wechat_channels": WeChatChannelsAdapter,
}

PLATFORM_LABEL = {"douyin": "抖音", "bilibili": "B站", "wechat_channels": "视频号"}


def collect(api_key: str, args) -> dict:
    client = TikHubClient(api_key, debug=args.debug, min_interval=args.interval)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    run_at = datetime.now(CN_TZ).isoformat()
    previous = {"accounts": [], "videos": []}
    previous_path = Path(args.out)
    if previous_path.exists():
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    result = {
        "updated_at": run_at,
        "source": "tikhub",
        "refresh_scope": "all",
        "accounts": [],
        "videos": [],
        "api_calls": 0,
        "warnings": [],
    }
    for acc in cfg["accounts"]:
        key = f"{acc['platform']}:{acc['account_name']}"
        cached_videos = [v for v in previous.get("videos", []) if v.get("account_key") == key]
        cached_account = next((a for a in previous.get("accounts", [])
                               if a.get("account_key") == key), None)
        account_started = time.monotonic()
        calls_before = client.call_count
        print(f">>> 采集 {PLATFORM_LABEL[acc['platform']]} / {acc['business_line']} / {acc['account_name']}")
        entry = {**acc, "account_key": key,
                 "platform_label": PLATFORM_LABEL[acc["platform"]],
                 "followers": None, "total_videos": 0, "status": "ok", "error": "",
                 "last_attempt_at": run_at, "refreshed_in_run": False}
        try:
            if acc["platform"] == "bilibili":
                adapter = BilibiliAdapter(client, enrich=not args.no_enrich_bili)
            else:
                adapter = ADAPTERS[acc["platform"]](client)
            info = adapter.resolve_user(acc)
            print(f"    定位成功: {info.get('nickname')}  粉丝: {info.get('followers')}")
            if acc["platform"] == "douyin":
                vids = adapter.fetch_videos(info["sec_user_id"])
                entry["platform_uid"] = info.get("unique_id") or acc.get("platform_uid", "")
            elif acc["platform"] == "bilibili":
                vids = adapter.fetch_videos(info["mid"])
                entry["platform_uid"] = info["mid"]
            else:
                vids = adapter.fetch_videos(info["username"])
                entry["platform_uid"] = info["username"]
            if is_suspicious_drop(len(vids), len(cached_videos)):
                raise RuntimeError(
                    f"本次仅返回 {len(vids)} 条，较上次 {len(cached_videos)} 条异常下降；"
                    "为避免不完整响应覆盖历史快照，已中止替换")
            entry["followers"] = info.get("followers")
            entry["nickname"] = info.get("nickname")
            entry["total_videos"] = len(vids)
            entry["last_success_at"] = run_at
            entry["refreshed_in_run"] = True
            for v in vids:
                v.update({"account_key": key, "platform": acc["platform"],
                          "platform_label": PLATFORM_LABEL[acc["platform"]],
                          "business_line": acc["business_line"],
                          "account_name": acc["account_name"],
                          "snapshot_state": "current"})
            result["videos"].extend(vids)
            print(f"    作品数: {len(vids)}")
        except Exception as e:
            message = compact_error(e)
            if cached_account and cached_videos:
                # 单账号瞬时失败时保留上一次完整快照，避免本轮采集把线上大屏数据清空。
                entry.update({
                    "followers": cached_account.get("followers"),
                    "nickname": cached_account.get("nickname", acc["account_name"]),
                    "platform_uid": cached_account.get("platform_uid", acc.get("platform_uid", "")),
                    "total_videos": len(cached_videos),
                    "status": "stale",
                    "error": f"本次刷新失败，已保留上次数据：{message}",
                    "last_success_at": cached_account.get("last_success_at")
                                       or previous.get("updated_at"),
                })
                result["videos"].extend(
                    [{**video, "snapshot_state": "cached"} for video in cached_videos])
                print(f"    [警告] {entry['error']}", file=sys.stderr)
            else:
                entry["status"] = "error"
                entry["error"] = message
                print(f"    [错误] {entry['error']}", file=sys.stderr)
            result["warnings"].append({"account_key": key, "message": entry["error"]})
        entry["request_count"] = client.call_count - calls_before
        entry["collection_seconds"] = round(time.monotonic() - account_started, 2)
        result["accounts"].append(entry)
    result["api_calls"] = client.call_count
    result["ok_accounts"] = sum(1 for a in result["accounts"] if a["status"] == "ok")
    result["stale_accounts"] = sum(1 for a in result["accounts"] if a["status"] == "stale")
    result["error_accounts"] = sum(1 for a in result["accounts"] if a["status"] == "error")
    success_times = [a.get("last_success_at") for a in result["accounts"] if a.get("last_success_at")]
    result["data_as_of"] = min(success_times) if success_times else None
    result["latest_success_at"] = max(success_times) if success_times else None
    finalize_snapshot(result, "video")
    print(f"完成, 共调用 API {client.call_count} 次, 作品 {len(result['videos'])} 条")
    return result


def make_mock() -> dict:
    random.seed(42)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    topics = {
        "望获": ["望获OS 实时内核深度解析", "望获OS VxWorks 迁移实战", "工业现场总线适配教程",
                 "望获OS 在数控机床上的落地", "硬实时任务调度原理", "嵌入式虚拟化技术分享",
                 "望获OS 开发者大会回顾", "微内核与宏内核对比"],
        "芯片": ["RISC-V 安全启动全链路", "车规级 MCU 功能安全入门", "国密算法硬件加速实践",
                 "芯片 trusted boot 演示", "侧信道攻击与防护", "HSM 安全模块设计",
                 "汽车电子 ISO26262 解读", "安全芯片选型指南"],
    }
    now = datetime.now(CN_TZ)
    videos, accounts = [], []
    for acc in cfg["accounts"]:
        key = f"{acc['platform']}:{acc['account_name']}"
        n = random.randint(18, 42)
        if acc["platform"] == "wechat_channels":
            base = random.randint(800, 12000)
        else:
            base = random.randint(3000, 50000)
        entry = {**acc, "account_key": key,
                 "platform_label": PLATFORM_LABEL[acc["platform"]],
                 "followers": random.randint(1500, 68000), "total_videos": n,
                 "status": "ok", "error": "", "nickname": acc["account_name"]}
        accounts.append(entry)
        for i in range(n):
            day_offset = int(random.betavariate(1.6, 2.2) * 540)
            pub = now - timedelta(days=day_offset, hours=random.randint(0, 20))
            heat = random.betavariate(0.7, 5)
            if acc["platform"] == "wechat_channels":
                play = None
                like_base = base * heat * 0.35
            else:
                play = int(base * heat * random.uniform(0.4, 2.6))
                like_base = play
            like = int(like_base * random.uniform(0.015, 0.09))
            comment = int(like * random.uniform(0.08, 0.45))
            share = int(like * random.uniform(0.1, 0.6))
            collect = int(like * random.uniform(0.2, 0.9))
            stats = {"play": play, "like": like, "comment": comment,
                     "share": share, "collect": collect}
            if acc["platform"] == "douyin":
                stats["download"] = int(like * random.uniform(0.02, 0.2))
            if acc["platform"] == "bilibili":
                stats["danmaku"] = int(comment * random.uniform(0.5, 2.5))
                stats["coin"] = int(like * random.uniform(0.1, 0.5))
            title = f"{random.choice(topics[acc['business_line']])} 第{random.randint(1, 30)}期"
            videos.append({
                "account_key": key, "platform": acc["platform"],
                "platform_label": PLATFORM_LABEL[acc["platform"]],
                "business_line": acc["business_line"],
                "account_name": acc["account_name"],
                "video_id": f"mock_{acc['platform']}_{i}",
                "title": title, "cover": "", "url": "",
                "published_at": pub.isoformat(),
                "duration": random.randint(35, 720),
                "stats": stats,
            })
    return {"updated_at": now.isoformat(), "source": "mock",
            "accounts": accounts, "videos": videos}


def main():
    ap = argparse.ArgumentParser(description="TikHub 视频数据采集器")
    ap.add_argument("--mock", action="store_true", help="生成演示数据(不调用 API)")
    ap.add_argument("--debug", action="store_true", help="保存原始 API 响应到 data/debug/")
    ap.add_argument("--no-enrich-bili", action="store_true",
                    help="B站不逐条补全点赞/投币(节省额度)")
    ap.add_argument("--interval", type=float, default=0.6, help="API 调用最小间隔秒数")
    ap.add_argument("--out", default=str(OUT_PATH), help="输出 JSON 路径")
    args = ap.parse_args()

    if args.mock:
        data = finalize_snapshot(make_mock(), "video")
    else:
        load_dotenv()
        api_key = os.environ.get("TIKHUB_API_KEY", "").strip()
        if not api_key:
            sys.exit("未找到 TIKHUB_API_KEY。请 export TIKHUB_API_KEY=xxx 或写入 .env 文件。"
                     "（TikHub 用户中心 -> API令牌 创建）\n"
                     "临时预览大屏可用: python3 pipeline/fetch_data.py --mock")
        data = collect(api_key, args)

    out = Path(args.out)
    atomic_write_json(out, data, pretty=True)
    web_data = ROOT / "web" / "data" / "dashboard_data.json"
    atomic_write_json(web_data, data)
    # file:// 直开模式: 浏览器无法 fetch 本地 JSON, 注入为全局变量
    web_js = ROOT / "web" / "data" / "dashboard_data.js"
    atomic_write_text(
        web_js,
        "window.__DASHBOARD_DATA__ = " + json.dumps(data, ensure_ascii=False) + ";\n",
    )
    print(f"已写入 {out} 与 {web_data}(.js)")


if __name__ == "__main__":
    main()
