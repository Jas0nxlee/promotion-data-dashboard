#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图文推广数据采集器。

公开页面采集：CSDN、电子发烧友、百家号、今日头条、搜狐；
TikHub API 采集：知乎、公众号、小红书；其余平台可导入后台数据。
"""

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from snapshot_utils import (
    atomic_write_json,
    atomic_write_text,
    finalize_snapshot,
    is_suspicious_drop,
    merge_records,
)

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit(
        "缺少依赖，请运行: python3 -m venv .venv && "
        ".venv/bin/python -m pip install -r requirements.txt")


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "article_accounts.json"
OUT_PATH = ROOT / "data" / "article_dashboard_data.json"
MANUAL_PATH = ROOT / "data" / "article_manual_input.json"
DEBUG_DIR = ROOT / "data" / "debug" / "articles"
WEB_JSON_PATH = ROOT / "web" / "articles" / "data" / "article_dashboard_data.json"
WEB_JS_PATH = ROOT / "web" / "articles" / "data" / "article_dashboard_data.js"
TIKHUB_BASE_URL = "https://api.tikhub.io"
CN_TZ = timezone(timedelta(hours=8))

PLATFORM_LABEL = {
    "csdn": "CSDN",
    "elecfans": "电子发烧友",
    "baijiahao": "百家号",
    "zhihu": "知乎",
    "wechat_service": "服务号",
    "wechat_subscription": "订阅号",
    "toutiao": "今日头条",
    "sohu": "搜狐",
    "xiaohongshu": "小红书",
}


def load_dotenv():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def compact_error(exc, limit=400):
    text = re.sub(r"\s+", " ", str(exc)).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def account_key(account):
    return f"{account['platform']}:{account['account_name']}"


def to_int(value, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "").replace("+", "")
    multiplier = 1
    if "亿" in text:
        multiplier = 100_000_000
    elif "万" in text:
        multiplier = 10_000
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return default
    try:
        return int(float(match.group()) * multiplier)
    except ValueError:
        return default


def first_number(*values):
    nums = [to_int(value) for value in values]
    nums = [value for value in nums if value is not None]
    return max(nums) if nums else None


def epoch_to_iso(value):
    ts = to_int(value)
    if not ts:
        return None
    if ts > 10_000_000_000:
        ts //= 1000
    return datetime.fromtimestamp(ts, tz=CN_TZ).isoformat()


def datetime_to_iso(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        return epoch_to_iso(value)
    text = str(value).strip().replace("/", "-")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CN_TZ)
        return parsed.astimezone(CN_TZ).isoformat()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=CN_TZ).isoformat()
        except ValueError:
            continue
    return None


def relative_time_to_iso(value):
    text = str(value or "").strip().replace("\xa0", " ")
    # 搜狐历史列表会使用 2026.07.25，统一为标准日期分隔符。
    text = re.sub(r"(?<=\d)[./](?=\d)", "-", text)
    now = datetime.now(CN_TZ)
    match = re.search(r"(\d+)\s*分钟前", text)
    if match:
        return (now - timedelta(minutes=int(match.group(1)))).isoformat()
    match = re.search(r"(\d+)\s*小时前", text)
    if match:
        return (now - timedelta(hours=int(match.group(1)))).isoformat()
    match = re.search(r"(\d+)\s*天前", text)
    if match:
        return (now - timedelta(days=int(match.group(1)))).isoformat()
    match = re.search(r"昨天\s*(\d{1,2}):(\d{2})", text)
    if match:
        yesterday = now - timedelta(days=1)
        return yesterday.replace(hour=int(match.group(1)), minute=int(match.group(2)),
                                 second=0, microsecond=0).isoformat()
    match = re.search(r"前天\s*(\d{1,2}):(\d{2})", text)
    if match:
        day = now - timedelta(days=2)
        return day.replace(hour=int(match.group(1)), minute=int(match.group(2)),
                           second=0, microsecond=0).isoformat()
    match = re.search(r"(?:(\d{4})-)?(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", text)
    if match:
        year = int(match.group(1) or now.year)
        month, day = int(match.group(2)), int(match.group(3))
        hour, minute = int(match.group(4) or 0), int(match.group(5) or 0)
        candidate = datetime(year, month, day, hour, minute, tzinfo=CN_TZ)
        if not match.group(1) and candidate > now + timedelta(days=2):
            candidate = candidate.replace(year=year - 1)
        return candidate.isoformat()
    return datetime_to_iso(text)


def strip_html(value):
    if not value:
        return ""
    return BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)


def norm_text(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", strip_html(value or ""))).lower()


def dig(obj, *paths, default=None):
    for path in paths:
        current = obj
        found = True
        for key in path.split("."):
            if isinstance(current, dict) and key in current:
                current = current[key]
            elif isinstance(current, list) and key.isdigit() and int(key) < len(current):
                current = current[int(key)]
            else:
                found = False
                break
        if found and current is not None:
            return current
    return default


def unwrap_data(obj):
    current = obj
    for _ in range(5):
        if not isinstance(current, dict) or not isinstance(current.get("data"), dict):
            break
        if "request_id" in current or "router" in current or set(current).issubset(
                {"code", "message", "ttl", "data"}):
            current = current["data"]
        else:
            break
    return current


def unwrap_service_data(obj):
    """继续展开 TikHub 内层上游服务的 code/success/data 包装。"""
    current = unwrap_data(obj)
    for _ in range(3):
        if not isinstance(current, dict) or not isinstance(current.get("data"), dict):
            break
        if "success" in current or "debug_id" in current or set(current).issubset(
                {"code", "message", "msg", "success", "data"}):
            current = current["data"]
        else:
            break
    return current


def parse_json_after_marker(text, marker):
    """解析页面脚本中 marker 后紧随的 JSON 对象。"""
    pos = text.find(marker)
    if pos < 0:
        raise ValueError(f"页面缺少数据标记: {marker}")
    raw = text[pos + len(marker):].lstrip()
    payload, _ = json.JSONDecoder().raw_decode(raw)
    return payload


def with_query_params(url, **updates):
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in updates.items() if value is not None})
    return urlunparse(parsed._replace(query=urlencode(query)))


class HttpClient:
    def __init__(self, debug=False, min_interval=0.15):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0 Safari/537.36"),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        })
        self.debug = debug
        self.min_interval = min_interval
        self.last_call = 0.0
        self.call_count = 0

    def get(self, url, *, params=None, headers=None, timeout=30, retries=3, tag="page"):
        last_error = "未知错误"
        for attempt in range(1, retries + 1):
            wait = self.min_interval - (time.time() - self.last_call)
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.time()
            self.call_count += 1
            try:
                response = self.session.get(url, params=params, headers=headers, timeout=timeout)
                if response.status_code == 200:
                    if self.debug:
                        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                        path = DEBUG_DIR / f"{tag}_{int(time.time() * 1000)}.html"
                        path.write_text(response.text, encoding="utf-8")
                    return response
                last_error = f"HTTP {response.status_code}"
            except requests.RequestException as exc:
                last_error = compact_error(exc)
            if attempt < retries:
                time.sleep(min(1.5 * attempt, 4))
        raise RuntimeError(f"请求失败 {url}: {last_error}")

    def get_json(self, url, **kwargs):
        response = self.get(url, **kwargs)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"接口未返回 JSON: {url}") from exc
        if self.debug:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            tag = kwargs.get("tag", "json")
            path = DEBUG_DIR / f"{tag}_{int(time.time() * 1000)}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload


class TikHubClient:
    def __init__(self, api_key, debug=False, min_interval=0.6):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        })
        self.debug = debug
        self.min_interval = min_interval
        self.last_call = 0.0
        self.call_count = 0

    def _request(self, method, path, *, params=None, payload=None, timeout=35, retries=3, tag="api"):
        if not self.api_key:
            raise RuntimeError("缺少 TIKHUB_API_KEY，无法采集需要 TikHub 的账号")
        url = f"{TIKHUB_BASE_URL}{path}"
        last_error = "未知错误"
        for attempt in range(1, retries + 1):
            wait = self.min_interval - (time.time() - self.last_call)
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.time()
            self.call_count += 1
            try:
                response = self.session.request(method, url, params=params, json=payload, timeout=timeout)
                if response.status_code == 200:
                    payload = response.json()
                    if self.debug:
                        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                        path_out = DEBUG_DIR / f"{tag}_{int(time.time() * 1000)}.json"
                        path_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
                    return payload
                last_error = f"HTTP {response.status_code}: {response.text[:180]}"
                if response.status_code in (401, 403):
                    break
            except (requests.RequestException, ValueError) as exc:
                last_error = compact_error(exc)
            if attempt < retries:
                time.sleep(min(2 ** attempt, 5))
        raise RuntimeError(f"API 请求失败 {path}: {last_error}")

    def get(self, path, **kwargs):
        return self._request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self._request("POST", path, **kwargs)


def base_account(account):
    return {
        **account,
        "account_key": account_key(account),
        "platform_label": PLATFORM_LABEL.get(account["platform"], account["platform"]),
        "nickname": account["account_name"],
        "followers": None,
        "total_articles": None,
        "covered_articles": 0,
        "lifetime_reads": None,
        "lifetime_likes": None,
        "lifetime_comments": None,
        "lifetime_collects": None,
        "status": "ok",
        "error": "",
        "coverage_note": "",
    }


def attach_account(article, account):
    article.update({
        "account_key": account_key(account),
        "platform": account["platform"],
        "platform_label": PLATFORM_LABEL.get(account["platform"], account["platform"]),
        "business_line": account["business_line"],
        "account_name": account["account_name"],
        "owner": account.get("owner", ""),
        "content_type": account.get("content_type", "图文"),
    })
    return article


class CsdnCollector:
    def __init__(self, public_client, max_pages=80):
        self.http = public_client
        self.max_pages = max_pages

    def profile(self, username):
        result = {}
        for _ in range(3):
            try:
                response = self.http.get(
                    f"https://blog.csdn.net/{username}",
                    params={"_": f"{int(time.time() * 1000)}{random.randint(10, 99)}"},
                    retries=1, tag=f"csdn_profile_{username}")
                marker = "window.__INITIAL_STATE__="
                pos = response.text.find(marker)
                if pos < 0:
                    continue
                raw = response.text[pos + len(marker):].lstrip()
                state, _ = json.JSONDecoder().raw_decode(raw)
                info = dig(state, "pageData.data.baseInfo", default={}) or {}
                user = info.get("userModule", {})
                achievement = info.get("achievementModule", {})
                result = {
                    "nickname": user.get("nickname"),
                    "followers": to_int(achievement.get("fansCount")),
                    "profile_original_articles": to_int(achievement.get("originalCount")),
                    "lifetime_reads": to_int(dig(achievement, "wholeSiteViewCount.total")),
                }
                for item in achievement.get("achievementList", []) or []:
                    template = item.get("template", "")
                    value = to_int(item.get("variable"))
                    if "点赞" in template:
                        result["lifetime_likes"] = value
                    elif "评论" in template:
                        result["lifetime_comments"] = value
                    elif "收藏" in template:
                        result["lifetime_collects"] = value
                return result
            except (RuntimeError, ValueError, KeyError, json.JSONDecodeError):
                time.sleep(0.2)
        return result

    def collect(self, account):
        username = account.get("platform_uid", "").strip()
        if not username:
            raise RuntimeError("缺少 CSDN username")
        entry = base_account(account)
        profile = self.profile(username)
        entry.update({key: value for key, value in profile.items() if value is not None})

        page_size = 100
        articles, seen = [], set()
        api_total = None
        page_error = ""
        page = 1
        while page <= self.max_pages:
            try:
                payload = self.http.get_json(
                    "https://blog.csdn.net/community/home-api/v1/get-business-list",
                    params={
                        "page": page,
                        "size": page_size,
                        "businessType": "blog",
                        "orderby": "",
                        "noMore": "false",
                        "year": "",
                        "month": "",
                        "username": username,
                        "_": f"{int(time.time() * 1000)}{page}",
                    },
                    headers={
                        "Referer": f"https://blog.csdn.net/{username}",
                        "Accept": "application/json, text/plain, */*",
                    },
                    tag=f"csdn_articles_{username}_{page}")
                data = payload.get("data", {}) if isinstance(payload, dict) else {}
                items = data.get("list", []) or []
                if page == 1:
                    api_total = to_int(data.get("total"), len(items))
                if not items:
                    break
                for item in items:
                    article_id = str(item.get("articleId") or "")
                    if not article_id or article_id in seen:
                        continue
                    seen.add(article_id)
                    tags = [tag.get("name", "") if isinstance(tag, dict) else str(tag)
                            for tag in (item.get("tags") or [])]
                    pictures = item.get("picList") or []
                    cover = ""
                    if pictures:
                        cover = pictures[0].get("url", "") if isinstance(pictures[0], dict) \
                            else str(pictures[0])
                    article = {
                        "article_id": article_id,
                        "title": item.get("title", ""),
                        "cover": cover,
                        "url": item.get("url") or f"https://blog.csdn.net/{username}/article/details/{article_id}",
                        "published_at": datetime_to_iso(item.get("postTime")),
                        "summary": strip_html(item.get("description", "")),
                        "tags": tags,
                        "stats": {
                            "read": to_int(item.get("viewCount")),
                            "like": to_int(item.get("diggCount")),
                            "comment": to_int(item.get("commentCount")),
                            "share": None,
                            "collect": to_int(item.get("collectCount")),
                        },
                    }
                    articles.append(attach_account(article, account))
                if len(items) < page_size or len(articles) >= (api_total or len(articles)):
                    break
                page += 1
            except Exception as exc:
                if not articles:
                    raise
                page_error = f"第 {page} 页采集失败：{compact_error(exc)}"
                break

        total = max(api_total or 0, len(articles))
        entry["total_articles"] = total
        entry["listed_articles"] = api_total
        entry["covered_articles"] = len(articles)
        if page_error or len(articles) < total:
            entry["status"] = "partial"
            entry["error"] = page_error or "公开文章列表未完整返回"
        entry["coverage_note"] = (
            f"CSDN 博客列表覆盖 {len(articles)}/{total} 篇；"
            f"主页原创累计 {entry.get('profile_original_articles') or '-'} 篇"
        )
        return entry, articles


class ElecfansCollector:
    def __init__(self, public_client, max_pages=80):
        self.http = public_client
        self.max_pages = max_pages

    @staticmethod
    def parse_page(html, account):
        soup = BeautifulSoup(html, "html.parser")
        rows = []
        for item in soup.select("li"):
            link = item.select_one('.art-list-top a[href*="/d/"]')
            if not link:
                continue
            url = urljoin("https://www.elecfans.com", link.get("href", ""))
            id_match = re.search(r"/d/(\d+)\.html", url)
            if not id_match:
                continue
            title_node = link.select_one("h3")
            time_node = (item.select_one(".art-list-top .time span[title]")
                         or item.select_one(".art-list-top .time"))
            summary_node = item.select_one(".answer-font")
            image_node = item.select_one(".answer-content img")
            like_node = item.select_one(".art-detail .art-follow span")
            read_value, comment_value = None, None
            for node in item.select(".art-detail .art-cate span"):
                text = node.get_text(" ", strip=True)
                if "阅读" in text:
                    read_value = to_int(text)
                elif "评论" in text:
                    comment_value = to_int(text)
            article = {
                "article_id": id_match.group(1),
                "title": title_node.get_text(" ", strip=True) if title_node else link.get_text(" ", strip=True),
                "cover": (image_node.get("src") or image_node.get("data-src") or "") if image_node else "",
                "url": url,
                "published_at": datetime_to_iso(
                    time_node.get("title") or time_node.get_text(" ", strip=True)
                ) if time_node else None,
                "summary": summary_node.get_text(" ", strip=True) if summary_node else "",
                "tags": [],
                "stats": {
                    "read": read_value,
                    "like": to_int(like_node.get_text(" ", strip=True)) if like_node else None,
                    "comment": comment_value,
                    "share": None,
                    "collect": None,
                },
            }
            rows.append(attach_account(article, account))
        page_numbers = []
        for link in soup.select('a[href*="/articles/"]'):
            match = re.search(r"/articles/(\d+)/?", link.get("href", ""))
            if match:
                page_numbers.append(int(match.group(1)))
        return rows, max(page_numbers, default=1)

    def collect(self, account):
        uid = account.get("platform_uid", "").strip()
        base_url = account.get("profile_url") or f"https://bbs.elecfans.com/user/{uid}/articles/"
        if not uid:
            raise RuntimeError("缺少电子发烧友用户 ID")
        entry = base_account(account)
        articles, seen = [], set()
        first = self.http.get(base_url, tag=f"elecfans_{uid}_1")
        first_rows, discovered_last_page = self.parse_page(first.text, account)
        for article in first_rows:
            seen.add(article["article_id"])
            articles.append(article)
        last_page = min(discovered_last_page, self.max_pages)
        page_capped = discovered_last_page > self.max_pages
        page_error = ""
        pages_collected = 1
        for page in range(2, last_page + 1):
            try:
                page_url = f"{base_url.rstrip('/')}/{page}/"
                response = self.http.get(page_url, tag=f"elecfans_{uid}_{page}")
                pages_collected = page
                rows, _ = self.parse_page(response.text, account)
                if not rows:
                    if page < discovered_last_page:
                        page_error = f"第 {page} 页为空，公开分页可能未完整返回"
                    break
                new_rows = [row for row in rows if row["article_id"] not in seen]
                if not new_rows:
                    break
                for article in new_rows:
                    seen.add(article["article_id"])
                    articles.append(article)
            except Exception as exc:
                page_error = f"第 {page} 页采集失败：{compact_error(exc)}"
                break
        if not articles:
            raise RuntimeError("电子发烧友公开主页未解析到文章")
        entry["total_articles"] = None if (page_error or page_capped) else len(articles)
        entry["covered_articles"] = len(articles)
        entry["pages_collected"] = pages_collected
        entry["pages_available"] = discovered_last_page
        entry["coverage_note"] = (
            f"公开主页已采集 {len(articles)} 篇（{entry['pages_collected']} 页）")
        if page_error or page_capped:
            entry["status"] = "partial"
            entry["error"] = page_error or (
                f"公开主页共有 {discovered_last_page} 页，本次最多采集 {self.max_pages} 页")
            entry["coverage_note"] += "，仍有分页未覆盖"
        return entry, articles


class BaijiahaoCollector:
    """采集百家号公开作者页及文章标签页。"""

    BAIDU_APP_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Version/4.0 "
            "Chrome/112.0.0.0 Mobile Safari/537.36 baiduboxapp/13.58.0.10"
        ),
    }

    def __init__(self, public_client, max_pages=80):
        self.http = public_client
        self.max_pages = max(1, max_pages)

    def collect(self, account):
        uid = (account.get("platform_uid") or "").strip()
        if not uid:
            raise RuntimeError("缺少百家号作者 ID")
        profile_url = account.get("profile_url") or f"https://author.baidu.com/home/{uid}"
        profile_response = self.http.get(
            profile_url, headers=self.BAIDU_APP_HEADERS, tag=f"baijiahao_profile_{uid}")
        runtime = parse_json_after_marker(profile_response.text, "window.runtime=")
        user = runtime.get("user", {}) if isinstance(runtime, dict) else {}
        nickname = user.get("nickname") or ""
        if norm_text(nickname) != norm_text(account["account_name"]):
            raise RuntimeError(
                f"百家号身份不匹配：配置为“{account['account_name']}”，页面为“{nickname or '未知'}”")

        article_url = next((
            item.get("url") for item in (user.get("tabs") or [])
            if isinstance(item, dict) and item.get("url") and "tab=article" in item["url"]
        ), None)
        if not article_url:
            raise RuntimeError("百家号主页未提供文章标签页")

        entry = base_account(account)
        entry.update({
            "nickname": nickname,
            "followers": to_int(user.get("fans_num")),
            "lifetime_likes": to_int(user.get("likes_num")),
            "profile_content_total": to_int(user.get("content_num")),
            "profile_url": profile_url,
        })

        articles, seen = [], set()
        cursor = None
        has_more = False
        page_error = ""
        request_headers = {**self.BAIDU_APP_HEADERS, "Referer": profile_url}
        for page in range(1, self.max_pages + 1):
            page_url = with_query_params(article_url, ctime=cursor) if cursor else article_url
            try:
                response = self.http.get(
                    page_url, headers=request_headers,
                    tag=f"baijiahao_articles_{uid}_{page}")
                payload = parse_json_after_marker(response.text, "window.dynamicData=")
                if payload.get("foe", {}).get("is_need_foe"):
                    raise RuntimeError("百家号触发访问校验")
                items = payload.get("list") or []
                for row in items:
                    if not isinstance(row, dict) or row.get("itemType") != "article":
                        continue
                    item = row.get("itemData") or {}
                    article_id = str(
                        item.get("shoubai_c_articleid") or item.get("article_id")
                        or item.get("feed_id") or row.get("feed_id") or "")
                    if not article_id or article_id in seen:
                        continue
                    seen.add(article_id)
                    images = item.get("imgSrc") or []
                    cover = ""
                    if images and isinstance(images[0], dict):
                        cover = images[0].get("content_original") or images[0].get("src") or ""
                    articles.append(attach_account({
                        "article_id": article_id,
                        "title": item.get("title") or "",
                        "cover": cover,
                        "url": item.get("url") or "",
                        "published_at": epoch_to_iso(
                            item.get("publish_at") or item.get("created_at")
                            or row.get("dynamic_ctime")),
                        "summary": strip_html(item.get("subtitle") or ""),
                        "tags": [],
                        "stats": {
                            "read": None,
                            "like": None,
                            "comment": None,
                            "share": None,
                            "collect": None,
                        },
                    }, account))
                has_more = bool(payload.get("hasMore"))
                next_cursor = dig(payload, "query.ctime")
                if not has_more or not items or not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
            except Exception as exc:
                if not articles:
                    raise
                page_error = f"第 {page} 页采集失败：{compact_error(exc)}"
                has_more = True
                break

        entry["total_articles"] = len(articles)
        entry["covered_articles"] = len(articles)
        content_total = entry.get("profile_content_total")
        entry["coverage_note"] = (
            f"文章标签页采集 {len(articles)} 篇；主页累计内容 "
            f"{content_total if content_total is not None else '-'} 条（含其他体裁）；"
            "文章列表未公开逐篇互动"
        )
        if has_more or page_error:
            entry["status"] = "partial"
            entry["error"] = page_error or f"达到最大翻页数 {self.max_pages}，仍有历史文章"
        return entry, articles


class ZhihuCollector:
    def __init__(self, api_client, max_pages=80):
        self.api = api_client
        self.max_pages = max_pages

    def collect(self, account):
        token = account.get("platform_uid", "").strip()
        if not token:
            raise RuntimeError("缺少知乎 URL token")
        info = unwrap_data(self.api.get(
            "/api/v1/zhihu/web/fetch_user_info",
            params={"user_url_token": token}, tag=f"zhihu_profile_{token}"))
        profile_error = ""
        if isinstance(info, dict) and isinstance(info.get("error"), dict):
            message = info["error"].get("message") or info["error"].get("name") or "未知限制"
            profile_error = f"用户资料接口受限：{message}"
            info = {}
        elif not isinstance(info, dict) or not info.get("name"):
            profile_error = "用户资料接口响应无效"
            info = {}
        elif norm_text(info.get("name")) != norm_text(account["account_name"]):
            raise RuntimeError(
                f"知乎身份不匹配：配置为“{account['account_name']}”，接口为“{info.get('name')}”")
        entry = base_account(account)
        entry.update({
            "nickname": info.get("name") or account["account_name"],
            "followers": to_int(info.get("follower_count")),
            "total_articles": to_int(info.get("articles_count")),
            "profile_url": account.get("profile_url") or f"https://www.zhihu.com/people/{token}",
        })

        articles, seen = [], set()
        author_verified = bool(info.get("name"))
        offset, limit = 0, 20
        for _ in range(self.max_pages):
            payload = unwrap_data(self.api.get(
                "/api/v1/zhihu/web/fetch_user_articles",
                params={"user_url_token": token, "offset": offset,
                        "limit": limit, "sort_type": "created"},
                tag=f"zhihu_articles_{token}_{offset}"))
            if not isinstance(payload, dict):
                raise RuntimeError("知乎文章列表响应无效")
            items = payload.get("data", []) or []
            for item in items:
                author = item.get("author") or {}
                author_token = str(author.get("url_token") or "")
                author_name = author.get("name") or ""
                if author_token and author_token != token:
                    raise RuntimeError(
                        f"知乎文章作者标识不匹配：配置为 {token}，文章返回 {author_token}")
                if author_name and norm_text(author_name) != norm_text(account["account_name"]):
                    raise RuntimeError(
                        f"知乎文章作者不匹配：配置为“{account['account_name']}”，"
                        f"文章返回“{author_name}”")
                if author_name:
                    author_verified = True
                    entry["nickname"] = author_name
                article_id = str(item.get("id") or "")
                if not article_id or article_id in seen:
                    continue
                seen.add(article_id)
                reaction = dig(item, "reaction.statistics", default={}) or {}
                article = {
                    "article_id": article_id,
                    "title": item.get("title", ""),
                    "cover": item.get("image_url", "") or "",
                    "url": (item.get("url") or f"https://zhuanlan.zhihu.com/p/{article_id}").replace("http://", "https://"),
                    "published_at": epoch_to_iso(item.get("created")),
                    "summary": strip_html(item.get("excerpt", "")),
                    "tags": [],
                    "stats": {
                        "read": None,
                        "like": first_number(item.get("voteup_count"), reaction.get("like_count")),
                        "comment": to_int(item.get("comment_count")),
                        "share": None,
                        "collect": to_int(reaction.get("favorites")),
                    },
                }
                articles.append(attach_account(article, account))
            paging = payload.get("paging", {}) or {}
            total = to_int(paging.get("totals"))
            if total is not None:
                entry["total_articles"] = total
            if paging.get("is_end") or not items:
                break
            offset += len(items)
        if not author_verified:
            raise RuntimeError(profile_error or "知乎账号身份无法核验")
        total = entry.get("total_articles")
        entry["total_articles"] = total if total is not None else len(articles)
        entry["covered_articles"] = len(articles)
        entry["coverage_note"] = f"文章接口返回 {len(articles)}/{entry['total_articles']} 篇；"
        if len(articles) < entry["total_articles"]:
            entry["coverage_note"] += "差额可能为不可见内容；"
        entry["coverage_note"] += "阅读量未公开"
        issues = []
        if profile_error:
            issues.append(profile_error)
            entry["coverage_note"] += "；资料指标不可用，身份已由文章作者字段核验"
        if len(articles) < entry["total_articles"]:
            issues.append("公开文章接口未返回全部文章")
        if issues:
            entry["status"] = "partial"
            entry["error"] = "；".join(issues)
        return entry, articles


class XiaohongshuCollector:
    """TikHub 小红书 APP V2：用户资料与已发布笔记。"""

    def __init__(self, api_client, max_pages=80):
        self.api = api_client
        self.max_pages = max(1, max_pages)

    @staticmethod
    def inner_payload(payload):
        return unwrap_service_data(payload)

    def collect(self, account):
        user_id = (account.get("platform_uid") or "").strip()
        if not user_id:
            raise RuntimeError("缺少小红书内部用户 ID")
        info = self.inner_payload(self.api.get(
            "/api/v1/xiaohongshu/app_v2/get_user_info",
            params={"user_id": user_id}, tag=f"xiaohongshu_profile_{user_id}"))
        if not isinstance(info, dict) or not info.get("nickname"):
            raise RuntimeError("小红书用户信息响应无效")
        nickname = info.get("nickname") or ""
        if norm_text(nickname) != norm_text(account["account_name"]):
            raise RuntimeError(
                f"小红书身份不匹配：配置为“{account['account_name']}”，接口为“{nickname}”")
        provided_id = (account.get("provided_id") or "").strip()
        api_red_id = str(info.get("red_id") or "")
        if provided_id and api_red_id and provided_id != api_red_id:
            raise RuntimeError(
                f"小红书号不匹配：配置为 {provided_id}，接口为 {api_red_id}")

        note_stat = info.get("note_num_stat") or {}
        entry = base_account(account)
        entry.update({
            "nickname": nickname,
            "followers": to_int(info.get("fans")),
            "total_articles": to_int(note_stat.get("posted")),
            "lifetime_likes": to_int(note_stat.get("liked")),
            "lifetime_collects": to_int(note_stat.get("collected")),
            "profile_url": account.get("profile_url")
                           or f"https://www.xiaohongshu.com/user/profile/{user_id}",
        })

        articles, seen = [], set()
        cursor = ""
        has_more = False
        page_error = ""
        for page in range(1, self.max_pages + 1):
            try:
                payload = self.inner_payload(self.api.get(
                    "/api/v1/xiaohongshu/app_v2/get_user_posted_notes",
                    params={"user_id": user_id, "cursor": cursor},
                    tag=f"xiaohongshu_notes_{user_id}_{page}"))
                if not isinstance(payload, dict):
                    raise RuntimeError("小红书笔记列表响应无效")
                items = payload.get("notes") or []
                for item in items:
                    note_id = str(item.get("id") or "")
                    if not note_id or note_id in seen:
                        continue
                    seen.add(note_id)
                    images = item.get("images_list") or []
                    cover = ""
                    if images and isinstance(images[0], dict):
                        cover = images[0].get("url_size_large") or images[0].get("url") or ""
                    articles.append(attach_account({
                        "article_id": note_id,
                        "title": item.get("title") or item.get("display_title") or "无标题笔记",
                        "cover": cover,
                        "url": f"https://www.xiaohongshu.com/explore/{note_id}",
                        "published_at": epoch_to_iso(item.get("create_time")),
                        "summary": item.get("desc") or "",
                        "tags": [],
                        "stats": {
                            # 公开响应中的 view_count 恒为 0，不能当作真实阅读量。
                            "read": None,
                            "like": to_int(item.get("likes")),
                            "comment": to_int(item.get("comments_count")),
                            "share": to_int(item.get("share_count")),
                            "collect": to_int(item.get("collected_count")),
                        },
                    }, account))
                has_more = bool(payload.get("has_more"))
                next_cursor = str(items[-1].get("cursor") or "") if items else ""
                if not has_more or not items or not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
            except Exception as exc:
                if not articles:
                    raise
                page_error = f"第 {page} 页采集失败：{compact_error(exc)}"
                has_more = True
                break

        total = entry.get("total_articles")
        entry["total_articles"] = total if total is not None else len(articles)
        entry["covered_articles"] = len(articles)
        entry["coverage_note"] = (
            f"笔记接口返回 {len(articles)}/{entry['total_articles']} 篇；"
            "阅读量不公开，互动按笔记列表口径"
        )
        if has_more or page_error or len(articles) < entry["total_articles"]:
            entry["status"] = "partial"
            entry["error"] = page_error or "公开笔记列表未覆盖主页累计发布数"
        return entry, articles


class SohuCollector:
    def __init__(self, public_client, max_pages=80):
        self.http = public_client

    def collect(self, account):
        uid = account.get("platform_uid", "").strip()
        url = account.get("profile_url") or f"https://m.sohu.com/media/{uid}"
        if not uid:
            raise RuntimeError("缺少搜狐作者 ID")
        response = self.http.get(url, tag=f"sohu_profile_{uid}")
        soup = BeautifulSoup(response.text, "html.parser")
        entry = base_account(account)
        profile_stats = {}
        for item in soup.select(".article-read-content"):
            number_node = item.select_one(".article-read-num")
            label_node = item.select_one(".article-read-text")
            if number_node and label_node:
                profile_stats[label_node.get_text(" ", strip=True)] = to_int(
                    number_node.get_text(" ", strip=True))
        entry.update({
            "followers": profile_stats.get("订阅"),
            "total_articles": profile_stats.get("内容"),
            "lifetime_reads": profile_stats.get("阅读"),
            "lifetime_likes": profile_stats.get("获赞"),
        })

        articles, seen = [], set()
        for item in soup.select(".feed-item"):
            link = item.select_one('a[href*="/a/"]')
            title_node = item.select_one(".title")
            if not link or not title_node:
                continue
            article_url = urljoin("https://m.sohu.com", link.get("href", ""))
            id_match = re.search(r"/a/(\d+)_", article_url)
            if not id_match or id_match.group(1) in seen:
                continue
            article_id = id_match.group(1)
            seen.add(article_id)
            image_node = item.select_one("img")
            extra_node = item.select_one(".extra-info-list")
            extra = extra_node.get_text(" ", strip=True).replace("\xa0", " ") if extra_node else ""
            read_match = re.search(r"([\d,.万亿]+)\s*阅读", extra)
            comment_match = re.search(r"([\d,.万亿]+)\s*评论", extra)
            time_text = extra.split("·", 1)[0].strip() if extra else ""
            article = {
                "article_id": article_id,
                "title": title_node.get_text(" ", strip=True),
                "cover": ((image_node.get("data-src") or image_node.get("src") or "")
                          if image_node else ""),
                "url": article_url.split("?", 1)[0],
                "published_at": relative_time_to_iso(time_text),
                "summary": "",
                "tags": [],
                "stats": {
                    "read": to_int(read_match.group(1)) if read_match else None,
                    "like": None,
                    "comment": to_int(comment_match.group(1)) if comment_match else None,
                    "share": None,
                    "collect": None,
                },
            }
            articles.append(attach_account(article, account))
        if not articles:
            raise RuntimeError("搜狐公开主页未解析到文章")
        total = entry.get("total_articles") or len(articles)
        entry["total_articles"] = total
        entry["covered_articles"] = len(articles)
        entry["coverage_note"] = f"公开主页覆盖最近 {len(articles)} 篇，账号累计 {total} 篇"
        if len(articles) < total:
            entry["status"] = "partial"
            entry["error"] = "公开主页仅稳定提供最近文章列表"
        return entry, articles


class WeChatMPCollector:
    """TikHub 公众号 V2：账号搜索 + 公众号历史文章分页。"""

    def __init__(self, api_client, manual_collector, max_pages=5, resolve_names=False,
                 stats_limit=0):
        self.api = api_client
        self.manual = manual_collector
        self.max_pages = max(1, max_pages)
        self.resolve_names = resolve_names
        self.stats_limit = max(0, stats_limit)

    def resolve_username(self, account):
        username = (account.get("platform_uid") or "").strip()
        if username.startswith("gh_"):
            return username
        if not self.resolve_names:
            return None
        payload = unwrap_service_data(self.api.post(
            "/api/v1/wechat_search/v2/fetch_search",
            payload={
                "keyword": account["account_name"],
                "business_type": "account",
                "sort": "default",
                "publish_time": "all",
                "offset": 0,
                "raw": False,
            },
            timeout=45,
            tag=f"wechat_search_{account['account_name']}",
        ))
        items = payload.get("items", []) if isinstance(payload, dict) else []
        target = norm_text(account["account_name"])
        for item in items:
            jump = item.get("jumpInfo") or item.get("jump_info") or {}
            nickname = jump.get("nickName") or jump.get("nick_name") or item.get("title")
            candidate = jump.get("userName") or jump.get("user_name")
            if candidate and candidate.startswith("gh_") and norm_text(nickname) == target:
                return candidate
        return None

    def collect(self, account):
        username = self.resolve_username(account)
        if not username:
            imported_entry, imported_articles = self.manual.collect(account)
            if imported_articles or imported_entry.get("status") != "pending":
                return imported_entry, imported_articles
            imported_entry["error"] = (
                "公众号接口当前未解析到 gh_username；"
                "请在配置中补充，或放入平台后台导出数据"
            )
            imported_entry["coverage_note"] = "待配置公众号 gh_username"
            return imported_entry, []

        entry = base_account(account)
        entry["platform_uid"] = username
        articles, seen = [], set()
        complete_types = []
        incomplete_types = []
        page_errors = []
        type_counts = {}
        # 0=普通文章，8=图片消息；两个入口的数据互不包含。
        for item_show_type, type_label in ((0, "普通文章"), (8, "图片消息")):
            offset = None
            is_end = False
            before_count = len(articles)
            for page in range(1, self.max_pages + 1):
                body = {
                    "username": username,
                    "page_size": 20,
                    "item_show_type": item_show_type,
                    "raw": False,
                }
                if offset:
                    body["offset"] = offset
                try:
                    payload = unwrap_service_data(self.api.post(
                        "/api/v1/wechat_mp/v2/fetch_account_articles",
                        payload=body,
                        timeout=45,
                        tag=f"wechat_articles_{username}_{item_show_type}_{page}",
                    ))
                except Exception as exc:
                    if articles:
                        page_errors.append(
                            f"{type_label}第 {page} 页失败：{compact_error(exc)}")
                        is_end = False
                        break
                    raise
                if not isinstance(payload, dict):
                    raise RuntimeError("TikHub 公众号文章列表响应无效")
                items = payload.get("articles", []) or []
                for item in items:
                    message_id = str(item.get("app_msg_id") or item.get("appmsgid") or "")
                    idx = str(item.get("idx") or "1")
                    unique_id = f"{message_id}:{idx}:{item.get('url', '')}"
                    if unique_id in seen:
                        continue
                    seen.add(unique_id)
                    article_id = f"{message_id}-{idx}" if message_id else \
                        hashlib.sha1(unique_id.encode("utf-8")).hexdigest()[:20]
                    covers = item.get("covers") or []
                    if isinstance(covers, dict):
                        covers = list(covers.values())
                    cover = item.get("cover") or (covers[0] if covers else "")
                    if isinstance(cover, dict):
                        cover = cover.get("url") or cover.get("src") or ""
                    articles.append(attach_account({
                        "article_id": article_id,
                        "title": item.get("title", ""),
                        "cover": cover or "",
                        "url": item.get("url", "") or "",
                        "published_at": epoch_to_iso(item.get("create_time")),
                        "summary": strip_html(item.get("digest", "")),
                        "tags": [type_label],
                        "stats": {
                            "read": None,
                            "like": None,
                            "comment": None,
                            "share": None,
                            "collect": None,
                        },
                    }, account))
                is_end = bool(payload.get("is_end"))
                next_offset = payload.get("next_offset")
                # 上游偶尔会在空列表后仍返回游标；继续使用该游标只会产生空页。
                if not items:
                    is_end = True
                if is_end or not next_offset or not items or next_offset == offset:
                    break
                offset = next_offset
            type_counts[type_label] = len(articles) - before_count
            (complete_types if is_end else incomplete_types).append(type_label)

        entry["total_articles"] = len(articles)
        entry["covered_articles"] = len(articles)
        enriched = 0
        stats_errors = 0
        if self.stats_limit:
            latest = sorted(
                [item for item in articles if item.get("url")],
                key=lambda item: item.get("published_at") or "",
                reverse=True,
            )[:self.stats_limit]
            for article in latest:
                try:
                    stats = unwrap_service_data(self.api.post(
                        "/api/v1/wechat_mp/v2/fetch_article_stats",
                        payload={"url": article["url"], "raw": False},
                        timeout=45,
                        tag=f"wechat_stats_{username}",
                    ))
                    if isinstance(stats, dict):
                        article["stats"].update({
                            "read": to_int(stats.get("read_num")),
                            "like": to_int(stats.get("like_count")),
                            "comment": to_int(stats.get("comment_count")),
                            "share": to_int(stats.get("share_count")),
                            "collect": to_int(stats.get("collect_count")),
                        })
                        enriched += 1
                except Exception:
                    stats_errors += 1
        entry["stats_enriched_articles"] = enriched
        entry["coverage_note"] = (
            f"公众号接口采集 {len(articles)} 篇（普通文章 {type_counts.get('普通文章', 0)}、"
            f"图片消息 {type_counts.get('图片消息', 0)}）；互动指标补全 {enriched} 篇"
        )
        if incomplete_types:
            entry["status"] = "partial"
            entry["error"] = (
                f"为控制 API 额度，{('、'.join(incomplete_types))}最多采集 "
                f"{self.max_pages} 页")
            entry["coverage_note"] += "，仍有历史分页"
        if page_errors:
            entry["status"] = "partial"
            entry["error"] = (entry.get("error") + "；" if entry.get("error") else "") + \
                             "；".join(page_errors)
            entry["coverage_note"] += "，后续分页失败"
        if stats_errors:
            entry["status"] = "partial"
            entry["error"] = (entry.get("error") + "；" if entry.get("error") else "") + \
                             f"{stats_errors} 篇互动指标补全失败"
        return entry, articles


class ToutiaoCollector:
    """通过今日头条公开作者页采集账号资料与文章列表。

    头条作者页使用匿名 ttwid、动态作者 token 和浏览器生成的请求签名。
    采集器不保存这些短期标识，而是让页面脚本按正常访问流程生成并分页。
    """

    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    )
    FEED_PATH = "/api/pc/list/user/feed"

    def __init__(self, max_pages=20, timeout_ms=30_000, headed=False, debug=False):
        self.max_pages = max(1, max_pages)
        self.timeout_ms = max(5_000, timeout_ms)
        self.headed = headed
        self.debug = debug
        self.request_count = 0
        self._playwright = None
        self._browser = None
        self._context = None

    @staticmethod
    def _load_playwright():
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "缺少 Playwright，请运行 .venv/bin/python -m pip install -r requirements.txt；"
                "若本机没有 Chrome，再运行 python3 -m playwright install chromium"
            ) from exc
        return sync_playwright

    def _anonymous_cookies(self):
        """预置头条匿名会话，避免先落到只负责注册 ttwid 的空页面。"""
        session = requests.Session()
        session.headers.update({
            "User-Agent": self.USER_AGENT,
            "Origin": "https://www.toutiao.com",
            "Referer": "https://www.toutiao.com/",
        })
        try:
            response = session.post(
                "https://ttwid.bytedance.com/ttwid/union/register/",
                data=json.dumps({
                    "aid": 24,
                    "service": "www.toutiao.com",
                    "region": "cn",
                    "union": True,
                    "needFid": False,
                }, separators=(",", ":")),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            response.raise_for_status()
            callback_url = response.json().get("redirect_url")
            if callback_url:
                session.get(callback_url, timeout=15).raise_for_status()
        except (requests.RequestException, ValueError):
            # 失败时仍可由作者页脚本自行注册，不让预热成为单点故障。
            return []

        cookies = []
        for cookie in session.cookies:
            if cookie.name != "ttwid":
                continue
            cookies.append({
                "name": cookie.name,
                "value": cookie.value,
                "domain": ".toutiao.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            })
        return cookies

    def _ensure_browser(self):
        if self._context is not None:
            return
        sync_playwright = self._load_playwright()
        self._playwright = sync_playwright().start()
        launch_options = {
            "headless": not self.headed,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        try:
            # 优先使用本机 Chrome，避免部署机重复下载浏览器运行时。
            self._browser = self._playwright.chromium.launch(
                channel="chrome", **launch_options)
        except Exception:
            try:
                # Docker 镜像使用 --no-shell，只安装完整 Chromium；显式 channel
                # 可使用新版无头模式，避免额外携带一份 headless-shell。
                self._browser = self._playwright.chromium.launch(
                    channel="chromium", **launch_options)
            except Exception as exc:
                try:
                    self._browser = self._playwright.chromium.launch(**launch_options)
                except Exception:
                    self.close()
                    raise RuntimeError(
                        "无法启动 Chrome/Chromium；请安装 Chrome，或运行 "
                        "python3 -m playwright install chromium"
                    ) from exc
        self._context = self._browser.new_context(
            user_agent=self.USER_AGENT,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 1000},
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
        )
        cookies = self._anonymous_cookies()
        if cookies:
            self._context.add_cookies(cookies)

    def close(self):
        for resource_name in ("_context", "_browser"):
            resource = getattr(self, resource_name, None)
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
                setattr(self, resource_name, None)
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    @staticmethod
    def _profile_metrics(page):
        metrics = {}
        for text in page.locator(".relation-stat .stat-item").all_inner_texts():
            compact = re.sub(r"\s+", "", text)
            if "获赞" in compact:
                metrics["lifetime_likes"] = to_int(compact)
            elif "粉丝" in compact:
                metrics["followers"] = to_int(compact)
            elif "关注" in compact:
                metrics["following"] = to_int(compact)
        return metrics

    @staticmethod
    def _cover_url(item):
        candidates = [
            dig(item, "large_image_list.0.url"),
            dig(item, "middle_image.url"),
            dig(item, "image_list.0.url"),
            dig(item, "itemCell.imageList.0.url"),
        ]
        return next((str(value) for value in candidates if value), "")

    @staticmethod
    def _article_from_item(item, account):
        article_id = str(item.get("group_id") or item.get("item_id") or item.get("id") or "")
        if not article_id:
            return None
        title = (item.get("title") or item.get("feed_title") or "").strip()
        # “全部”流可能混入只有正文、没有标题的微头条；图文大屏只保留文章。
        if not title:
            return None
        counters = dig(item, "itemCell.itemCounter", default={}) or {}
        forward_info = item.get("forward_info") or {}
        url = (item.get("article_url") or item.get("display_url")
               or item.get("url") or item.get("share_url") or "")
        if url.startswith("//"):
            url = f"https:{url}"
        elif url.startswith("/"):
            url = urljoin("https://www.toutiao.com", url)
        article = {
            "article_id": article_id,
            "title": title,
            "cover": ToutiaoCollector._cover_url(item),
            "url": url,
            "published_at": epoch_to_iso(item.get("publish_time") or item.get("behot_time")),
            "summary": strip_html(item.get("abstract") or item.get("content") or ""),
            "tags": [],
            "stats": {
                "read": first_number(counters.get("readCount"), item.get("read_count")),
                "like": first_number(counters.get("diggCount"), item.get("digg_count"),
                                     item.get("like_count")),
                "comment": first_number(counters.get("commentCount"),
                                        item.get("comment_count")),
                "share": first_number(counters.get("shareCount"), item.get("share_count"),
                                      forward_info.get("forward_count")),
                "collect": first_number(counters.get("repinCount"), item.get("repin_count")),
            },
        }
        return attach_account(article, account)

    def _save_debug(self, uid, page_number, payload):
        if not self.debug:
            return
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        debug_path = DEBUG_DIR / (
            f"toutiao_feed_{uid}_{page_number}_{int(time.time() * 1000)}.json")
        debug_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _next_feed(self, page):
        last_error = "滚动后未触发文章分页接口"
        for _ in range(2):
            try:
                with page.expect_response(
                        lambda response: self.FEED_PATH in response.url,
                        timeout=self.timeout_ms) as response_info:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.mouse.wheel(0, 1800)
                return response_info.value
            except Exception as exc:
                last_error = compact_error(exc)
                page.wait_for_timeout(400)
        raise RuntimeError(f"今日头条分页失败：{last_error}")

    def collect(self, account):
        uid = (account.get("platform_uid") or account.get("provided_id") or "").strip()
        if not uid:
            raise RuntimeError("缺少今日头条作者 ID")
        self._ensure_browser()
        page = self._context.new_page()
        entry = base_account(account)
        profile_url = account.get("profile_url") or f"https://www.toutiao.com/c/user/{uid}/"
        entry["profile_url"] = profile_url
        articles, seen = [], set()
        has_more = False
        page_error = ""
        page_count = 0
        try:
            try:
                with page.expect_response(
                        lambda response: self.FEED_PATH in response.url,
                        timeout=self.timeout_ms) as response_info:
                    page.goto(profile_url, wait_until="domcontentloaded",
                              timeout=self.timeout_ms)
                response = response_info.value
            except Exception as exc:
                title = page.title() if not page.is_closed() else ""
                raise RuntimeError(
                    f"作者页未加载文章接口（{title or '空页面'}）：{compact_error(exc)}") from exc

            page.wait_for_timeout(500)
            nickname_locator = page.locator(".profile-info .detail .name").first
            nickname = nickname_locator.inner_text().strip() if nickname_locator.count() else ""
            if not nickname:
                match = re.match(r"(.+?)的头条主页", page.title())
                nickname = match.group(1).strip() if match else ""
            if norm_text(nickname) != norm_text(account["account_name"]):
                raise RuntimeError(
                    f"今日头条身份不匹配：配置为“{account['account_name']}”，"
                    f"页面为“{nickname or '未知'}”")
            entry["nickname"] = nickname
            entry.update(self._profile_metrics(page))

            # 主页默认是“全部”流，会混入视频和微头条。切到“文章”标签，
            # 让后续滚动分页都保持 pc_profile_article 口径。
            article_tab = page.get_by_text("文章", exact=True)
            if article_tab.count():
                try:
                    with page.expect_response(
                            lambda article_response:
                            self.FEED_PATH in article_response.url
                            and "category=pc_profile_article" in article_response.url,
                            timeout=self.timeout_ms) as article_response_info:
                        article_tab.first.click()
                    # 默认“全部”流也是真实发生的一次浏览器请求。
                    self.request_count += 1
                    response = article_response_info.value
                    page.wait_for_timeout(350)
                except Exception as exc:
                    raise RuntimeError(
                        f"今日头条未能切换到文章列表：{compact_error(exc)}") from exc

            while response is not None and page_count < self.max_pages:
                self.request_count += 1
                page_count += 1
                try:
                    payload = response.json()
                except Exception as exc:
                    raise RuntimeError("今日头条文章接口未返回有效 JSON") from exc
                self._save_debug(uid, page_count, payload)
                if not isinstance(payload, dict) or payload.get("message") != "success":
                    decision = payload.get("decision") if isinstance(payload, dict) else None
                    if decision:
                        raise RuntimeError("今日头条触发访问校验，请稍后重试或使用 --toutiao-headed")
                    raise RuntimeError(
                        f"今日头条文章接口异常：{compact_error(payload.get('message', payload))}")
                for item in payload.get("data") or []:
                    if not isinstance(item, dict):
                        continue
                    article = self._article_from_item(item, account)
                    if not article or article["article_id"] in seen:
                        continue
                    seen.add(article["article_id"])
                    articles.append(article)
                has_more = bool(payload.get("has_more"))
                if not has_more:
                    break
                if page_count >= self.max_pages:
                    break
                try:
                    response = self._next_feed(page)
                except Exception as exc:
                    page_error = compact_error(exc)
                    break
                page.wait_for_timeout(250)

            if not articles:
                raise RuntimeError("今日头条公开作者页未返回文章")
            entry["covered_articles"] = len(articles)
            entry["listed_articles"] = len(articles)
            entry["pages_collected"] = page_count
            if not has_more and not page_error:
                entry["total_articles"] = len(articles)
                entry["coverage_note"] = (
                    f"公开作者页完整采集 {len(articles)} 篇（{page_count} 页）；"
                    "逐篇阅读、点赞、评论、分享、收藏按页面公开值")
            else:
                entry["total_articles"] = None
                entry["status"] = "partial"
                entry["error"] = page_error or (
                    f"为控制访问频率，最多采集 {self.max_pages} 页")
                entry["coverage_note"] = (
                    f"公开作者页采集最近 {len(articles)} 篇（{page_count} 页），仍有历史分页；"
                    "逐篇指标按页面公开值")
            return entry, articles
        finally:
            page.close()


class ManualCollector:
    def __init__(self, path):
        self.path = Path(path)
        self.payload = {"accounts": [], "articles": []}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.payload = loaded
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"人工导入文件无效: {compact_error(exc)}") from exc

    def collect(self, account):
        key = account_key(account)
        entry = base_account(account)
        imported_account = next((item for item in self.payload.get("accounts", [])
                                 if item.get("account_key") == key), None)
        imported_articles = [item for item in self.payload.get("articles", [])
                             if item.get("account_key") == key]
        if not imported_account and not imported_articles:
            entry["status"] = "pending"
            entry["error"] = account.get("note") or "需补充主页地址或导入平台后台数据"
            entry["coverage_note"] = "尚未导入"
            return entry, []
        if imported_account:
            for field in ("nickname", "followers", "total_articles", "lifetime_reads",
                          "lifetime_likes", "lifetime_comments", "lifetime_collects", "profile_url"):
                if field in imported_account:
                    entry[field] = imported_account[field]
        articles = []
        for index, item in enumerate(imported_articles, start=1):
            stats_in = item.get("stats", {}) or {}
            article = {
                "article_id": str(item.get("article_id") or f"manual-{index}"),
                "title": item.get("title", "未命名文章"),
                "cover": item.get("cover", "") or "",
                "url": item.get("url", "") or "",
                "published_at": datetime_to_iso(item.get("published_at")),
                "summary": item.get("summary", "") or "",
                "tags": item.get("tags", []) or [],
                "stats": {key_name: to_int(stats_in.get(key_name))
                          for key_name in ("read", "like", "comment", "share", "collect")},
            }
            articles.append(attach_account(article, account))
        total = to_int(entry.get("total_articles"), len(articles))
        entry["total_articles"] = max(total, len(articles))
        entry["covered_articles"] = len(articles)
        entry["coverage_note"] = f"平台后台导入 {len(articles)}/{entry['total_articles']} 篇"
        if len(articles) < entry["total_articles"]:
            entry["status"] = "partial"
            entry["error"] = "导入文章少于平台累计文章数"
        return entry, articles


def load_previous(path):
    if not path.exists():
        return {"accounts": [], "articles": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"accounts": [], "articles": []}
    except (OSError, json.JSONDecodeError):
        return {"accounts": [], "articles": []}


def collect(args):
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    previous = load_previous(Path(args.out))
    run_at = datetime.now(CN_TZ).isoformat()
    public_client = HttpClient(debug=args.debug, min_interval=args.public_interval)
    api_client = TikHubClient(os.environ.get("TIKHUB_API_KEY", "").strip(),
                              debug=args.debug, min_interval=args.interval)
    manual = ManualCollector(args.manual_input)
    toutiao = ToutiaoCollector(
        args.toutiao_pages,
        timeout_ms=int(args.toutiao_timeout * 1000),
        headed=args.toutiao_headed,
        debug=args.debug,
    )
    collectors = {
        "csdn": CsdnCollector(public_client, args.max_pages),
        "elecfans": ElecfansCollector(public_client, args.max_pages),
        "baijiahao": BaijiahaoCollector(public_client, args.max_pages),
        "zhihu": ZhihuCollector(api_client, args.max_pages),
        "sohu": SohuCollector(public_client, args.max_pages),
        "xiaohongshu": XiaohongshuCollector(api_client, args.max_pages),
        "wechat_mp": WeChatMPCollector(api_client, manual,
                                        args.wechat_pages, args.resolve_wechat,
                                        args.wechat_stats_limit),
        "toutiao": toutiao,
        "manual": manual,
    }
    result = {
        "updated_at": run_at,
        "source": "live",
        "refresh_scope": list(args.only or []) or "all",
        "accounts": [],
        "articles": [],
        "api_calls": 0,
        "public_requests": 0,
        "browser_requests": 0,
        "warnings": [],
    }
    only = set(args.only or [])
    try:
        for account in config.get("accounts", []):
            key = account_key(account)
            label = PLATFORM_LABEL.get(account["platform"], account["platform"])
            cached_articles = [item for item in previous.get("articles", [])
                               if item.get("account_key") == key]
            cached_account = next((item for item in previous.get("accounts", [])
                                   if item.get("account_key") == key), None)
            if only and not ({key, account["platform"], account["account_name"]} & only):
                entry = dict(cached_account) if cached_account else base_account(account)
                entry["refreshed_in_run"] = False
                if cached_account and not entry.get("last_success_at"):
                    entry["last_success_at"] = previous.get("updated_at")
                result["accounts"].append(entry)
                result["articles"].extend(
                    [{**article, "snapshot_state": "cached"} for article in cached_articles])
                if entry.get("status") != "ok":
                    result["warnings"].append({
                        "account_key": key,
                        "status": entry.get("status", "pending"),
                        "message": entry.get("error") or entry.get("coverage_note", "未刷新"),
                    })
                continue
            print(f">>> 采集 {label} / {account['business_line']} / {account['account_name']}")
            account_started = time.monotonic()
            api_before = api_client.call_count
            public_before = public_client.call_count
            browser_before = toutiao.request_count
            try:
                collector_name = account.get("collector", "manual")
                if collector_name not in collectors:
                    raise RuntimeError(f"未知采集器: {collector_name}")
                entry, articles = collectors[collector_name].collect(account)
                if entry.get("status") == "ok" and is_suspicious_drop(
                        len(articles), len(cached_articles)):
                    raise RuntimeError(
                        f"本次仅返回 {len(articles)} 篇，较上次 {len(cached_articles)} 篇异常下降；"
                        "为避免不完整响应覆盖历史快照，已中止替换")
                articles = [{**article, "snapshot_state": "current"} for article in articles]
                restored = 0
                if (entry.get("status") == "partial" and cached_articles
                        and len(articles) < len(cached_articles)):
                    articles, restored = merge_records(
                        articles, cached_articles, "article_id")
                    entry["covered_articles"] = len(articles)
                    if entry.get("total_articles") is not None:
                        entry["total_articles"] = max(entry["total_articles"], len(articles))
                    entry["snapshot_mode"] = "live+cache"
                    entry["coverage_note"] = (
                        f"{entry.get('coverage_note', '')}；合并上次快照补回 "
                        f"{restored} 篇历史记录")
                entry["last_attempt_at"] = run_at
                entry["last_success_at"] = run_at
                entry["refreshed_in_run"] = True
                result["articles"].extend(articles)
                print(f"    状态: {entry['status']}，文章: {len(articles)}")
            except Exception as exc:
                message = compact_error(exc)
                entry = base_account(account)
                entry["last_attempt_at"] = run_at
                entry["refreshed_in_run"] = False
                if cached_account and cached_articles:
                    entry.update({field: cached_account.get(field) for field in (
                        "nickname", "followers", "total_articles", "lifetime_reads",
                        "lifetime_likes", "lifetime_comments", "lifetime_collects",
                        "profile_url", "platform_uid") if field in cached_account})
                    entry.update({
                        "covered_articles": len(cached_articles),
                        "status": "stale",
                        "error": f"本次刷新失败，已保留上次数据：{message}",
                        "coverage_note": cached_account.get("coverage_note", "使用上次快照"),
                        "last_success_at": cached_account.get("last_success_at")
                                           or previous.get("updated_at"),
                    })
                    result["articles"].extend(
                        [{**article, "snapshot_state": "cached"} for article in cached_articles])
                else:
                    entry["status"] = "error"
                    entry["error"] = message
                    entry["coverage_note"] = "采集失败"
                print(f"    [警告] {entry['error']}", file=sys.stderr)
            entry["request_counts"] = {
                "api": api_client.call_count - api_before,
                "public": public_client.call_count - public_before,
                "browser": toutiao.request_count - browser_before,
            }
            entry["collection_seconds"] = round(time.monotonic() - account_started, 2)
            if entry["status"] != "ok":
                result["warnings"].append({
                    "account_key": key,
                    "status": entry["status"],
                    "message": entry["error"] or entry["coverage_note"],
                })
            result["accounts"].append(entry)
    finally:
        toutiao.close()
    result["api_calls"] = api_client.call_count
    result["public_requests"] = public_client.call_count
    result["browser_requests"] = toutiao.request_count
    for status in ("ok", "partial", "pending", "stale", "error"):
        result[f"{status}_accounts"] = sum(1 for item in result["accounts"]
                                            if item.get("status") == status)
    result["accounts_with_articles"] = sum(1 for item in result["accounts"]
                                           if item.get("covered_articles", 0) > 0)
    success_times = [item.get("last_success_at") for item in result["accounts"]
                     if item.get("last_success_at")]
    result["data_as_of"] = min(success_times) if success_times else None
    result["latest_success_at"] = max(success_times) if success_times else None
    finalize_snapshot(result, "article")
    print(f"完成：{len(result['articles'])} 篇，公开请求 {public_client.call_count} 次，"
          f"浏览器文章请求 {toutiao.request_count} 次，API 请求 {api_client.call_count} 次")
    return result


def make_mock():
    random.seed(73)
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(CN_TZ)
    topics = {
        "望获": ["实时操作系统工程实践", "嵌入式 Linux 稳定性指南", "工业控制软件迁移方法"],
        "芯片": ["安全芯片选型指南", "车规 MCU 设计实践", "接口芯片应用笔记"],
        "土星云": ["边缘计算落地案例", "AI 推理终端部署", "数字孪生平台实践"],
        "环宇": ["航天电子系统设计", "高可靠计算平台", "复杂系统工程方法"],
    }
    accounts, articles = [], []
    for account in config.get("accounts", []):
        entry = base_account(account)
        count = random.randint(18, 48)
        entry.update({
            "followers": random.randint(100, 35000),
            "total_articles": count,
            "covered_articles": count,
            "coverage_note": "演示数据",
        })
        accounts.append(entry)
        for index in range(count):
            published = now - timedelta(days=random.randint(0, 520), hours=random.randint(0, 20))
            read = random.randint(30, 80000)
            article = {
                "article_id": f"mock-{account['platform']}-{index}",
                "title": f"{random.choice(topics[account['business_line']])} · 第 {index + 1} 篇",
                "cover": "",
                "url": "",
                "published_at": published.isoformat(),
                "summary": "",
                "tags": [],
                "stats": {
                    "read": read,
                    "like": int(read * random.uniform(0.01, 0.08)),
                    "comment": int(read * random.uniform(0.001, 0.01)),
                    "share": int(read * random.uniform(0.001, 0.02)),
                    "collect": int(read * random.uniform(0.002, 0.03)),
                },
            }
            articles.append(attach_account(article, account))
    return {
        "updated_at": now.isoformat(),
        "source": "mock",
        "accounts": accounts,
        "articles": articles,
        "api_calls": 0,
        "public_requests": 0,
        "warnings": [],
        "ok_accounts": len(accounts),
        "partial_accounts": 0,
        "pending_accounts": 0,
        "stale_accounts": 0,
        "error_accounts": 0,
        "accounts_with_articles": len(accounts),
    }


def write_outputs(data, out_path):
    atomic_write_json(out_path, data, pretty=True)
    atomic_write_json(WEB_JSON_PATH, data)
    atomic_write_text(
        WEB_JS_PATH,
        "window.__ARTICLE_DASHBOARD_DATA__ = " + json.dumps(data, ensure_ascii=False) + ";\n",
    )
    print(f"已写入 {out_path}、{WEB_JSON_PATH} 和 {WEB_JS_PATH}")


def main():
    parser = argparse.ArgumentParser(description="图文推广数据采集器")
    parser.add_argument("--mock", action="store_true", help="生成演示数据，不访问平台")
    parser.add_argument("--debug", action="store_true", help="保存原始响应到 data/debug/articles")
    parser.add_argument("--interval", type=float, default=0.6, help="API 请求最小间隔秒数")
    parser.add_argument("--public-interval", type=float, default=0.15,
                        help="公开页面请求最小间隔秒数")
    parser.add_argument("--max-pages", type=int, default=80, help="单账号最大翻页数")
    parser.add_argument("--wechat-pages", type=int, default=80,
                        help="每个公众号每种内容最多采集页数，每页最多 20 条（默认 80）")
    parser.add_argument("--resolve-wechat", action="store_true",
                        help="通过 TikHub 搜索缺少 gh_username 的公众号（会产生计费请求）")
    parser.add_argument("--wechat-stats-limit", type=int, default=0,
                        help="每个公众号补全最新 N 篇互动指标；每篇会产生一次计费请求（默认 0）")
    parser.add_argument("--toutiao-pages", type=int, default=20,
                        help="每个今日头条账号最多采集页数，每页通常 20 篇（默认 20）")
    parser.add_argument("--toutiao-timeout", type=float, default=30,
                        help="今日头条页面及分页等待超时秒数（默认 30）")
    parser.add_argument("--toutiao-headed", action="store_true",
                        help="显示今日头条采集浏览器，用于验证码或访问校验排查")
    parser.add_argument("--manual-input", default=str(MANUAL_PATH), help="人工导入 JSON 路径")
    parser.add_argument("--out", default=str(OUT_PATH), help="主输出 JSON 路径")
    parser.add_argument(
        "--only", action="append",
        help="仅刷新指定平台、账号名或 account_key；可重复使用，其他账号保留现有快照")
    args = parser.parse_args()
    load_dotenv()
    data = finalize_snapshot(make_mock(), "article") if args.mock else collect(args)
    write_outputs(data, Path(args.out))


if __name__ == "__main__":
    main()
