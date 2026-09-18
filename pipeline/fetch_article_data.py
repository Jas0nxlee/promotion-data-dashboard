#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图文推广数据采集器。

公开页面采集：CSDN、电子发烧友、百家号、今日头条、搜狐；
授权后台采集：知乎、公众号、小红书；其余平台保留公开直采。
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


from runtime import DATA, WEB, load_env as load_dotenv
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "article_accounts.json"
OUT_PATH = DATA / "article_dashboard_data.json"
MANUAL_PATH = DATA / "article_manual_input.json"
DEBUG_DIR = DATA / "debug" / "articles"
WEB_JSON_PATH = WEB / "articles" / "data" / "article_dashboard_data.json"
WEB_JS_PATH = WEB / "articles" / "data" / "article_dashboard_data.js"
from providers import ProviderRegistry
from providers.history import retain_known
from providers.health import record_verification
from providers.public_articles import (PUBLIC_ARTICLE_PLATFORMS, annotate_public_articles,
                                      record_public_article_verification)
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
    """继续展开 历史上游服务的 code/success/data 包装。"""
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
        response = self.http.get(
            f"https://blog.csdn.net/{username}", retries=1,
            tag=f"csdn_profile_{username}")
        try:
            state = parse_json_after_marker(response.text, "window.__INITIAL_STATE__=")
        except (ValueError, TypeError):
            raise RuntimeError("CSDN 主页缺少身份数据，可能需要人工安全验证") from None
        info = dig(state, "pageData.data.baseInfo", default={}) or {}
        user = info.get("userModule", {})
        if not isinstance(user, dict) or user.get("username") != username:
            raise RuntimeError("CSDN 主页 username 与配置不一致")
        blog = urlparse(user.get("blogUrl") or "")
        if blog.hostname != "blog.csdn.net" or blog.path.rstrip("/") != "/" + username:
            raise RuntimeError("CSDN 主页作者链接与配置不一致")
        achievement = info.get("achievementModule", {})
        result = {"verified_account_id": username, "data_source": "csdn_public",
                  "nickname": user.get("nickname"),
                  "followers": to_int(achievement.get("fansCount")),
                  "profile_original_articles": to_int(achievement.get("originalCount")),
                  "lifetime_reads": to_int(dig(achievement, "wholeSiteViewCount.total"))}
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

    def _get_page(self, username, page):
        # This public listing returned HTTP 521 at page 12 during a fast scan,
        # while the same page was readable later. Pace CSDN separately from
        # other public sites; retry only that server error on the same page.
        for attempt in range(3):
            quiet = 7 - (time.time() - getattr(self.http, "last_call", 0.0))
            if quiet > 0:
                time.sleep(quiet)
            try:
                return self.http.get_json(
                    "https://blog.csdn.net/community/home-api/v1/get-business-list",
                    params={
                        "page": page, "size": 100, "businessType": "blog",
                        "orderby": "", "noMore": "false", "year": "", "month": "",
                        "username": username, "_": f"{int(time.time() * 1000)}{page}",
                    },
                    headers={"Referer": f"https://blog.csdn.net/{username}",
                             "Accept": "application/json, text/plain, */*"},
                    retries=1, tag=f"csdn_articles_{username}_{page}")
            except RuntimeError as exc:
                if "HTTP 521" not in str(exc) or attempt == 2:
                    raise
                time.sleep(7 * (attempt + 1))

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
                payload = self._get_page(username, page)
                if not isinstance(payload, dict) or payload.get("code") != 200:
                    raise RuntimeError("CSDN 文章接口业务状态未成功")
                data = payload.get("data")
                if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                    raise RuntimeError("CSDN 文章列表结构缺失")
                items = data["list"]
                total_value = data.get("total")
                if isinstance(total_value, bool) or not re.fullmatch(r"\d+", str(total_value)):
                    raise RuntimeError("CSDN 文章总数缺失或无效")
                current_total = int(total_value)
                if page == 1:
                    api_total = current_total
                elif current_total != api_total:
                    raise RuntimeError("CSDN 文章总数在分页期间变化")
                if not items:
                    if len(articles) != api_total:
                        raise RuntimeError("CSDN 提前返回空页，尚未覆盖声明总数")
                    break
                page_articles, page_ids = [], set()
                for item in items:
                    if not isinstance(item, dict):
                        raise RuntimeError("CSDN 文章记录格式改变")
                    article_id = str(item.get("articleId") or "")
                    if not article_id.isdigit() or isinstance(item.get("articleId"), (bool, float)):
                        raise RuntimeError("CSDN 文章缺少精确稳定 ID")
                    parsed_url = urlparse(item.get("url") or "")
                    if (parsed_url.scheme != "https" or parsed_url.hostname != "blog.csdn.net"
                            or parsed_url.path != f"/{username}/article/details/{article_id}"):
                        raise RuntimeError("CSDN 文章作者或 URL ID 与当前账号不一致")
                    if article_id in page_ids:
                        raise RuntimeError("CSDN 同页出现重复文章 ID")
                    page_ids.add(article_id)
                    if article_id in seen:
                        continue
                    tags = [tag.get("name", "") if isinstance(tag, dict) else str(tag)
                            for tag in (item.get("tags") or [])]
                    pictures = item.get("picList") or []
                    cover = ""
                    if pictures:
                        cover = pictures[0].get("url", "") if isinstance(pictures[0], dict) \
                            else str(pictures[0])
                    article = {
                        "article_id": article_id,
                        "data_source": "csdn_public",
                        "source_author_id": username,
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
                    page_articles.append(attach_account(article, account))
                if not page_articles:
                    raise RuntimeError("CSDN 分页重复，没有新增文章")
                if len(articles) + len(page_articles) > api_total:
                    raise RuntimeError("CSDN 文章数超过声明总数")
                seen.update(row["article_id"] for row in page_articles)
                articles.extend(page_articles)
                if len(articles) == api_total:
                    break
                if len(items) < page_size:
                    raise RuntimeError("CSDN 提前返回短页，尚未覆盖声明总数")
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
    def page_identity(html, account):
        soup = BeautifulSoup(html, "html.parser")
        uid = str(account.get("platform_uid") or "")
        current = soup.select_one('.column-nav li.current a[href]')
        nickname = soup.select_one('.user-top .user-name')
        if current is None or nickname is None:
            raise RuntimeError("电子发烧友主页缺少当前作者身份，可能需要人工安全验证")
        target = urlparse(urljoin("https://bbs.elecfans.com/", current.get("href", "")))
        if target.hostname != "bbs.elecfans.com" or target.path.rstrip("/") != f"/user/{uid}/articles":
            raise RuntimeError("电子发烧友当前作者 UID 与项目配置不一致")
        total_node = current.parent.select_one('span')
        total_text = total_node.get_text(strip=True) if total_node else ""
        if not total_text.isdigit():
            raise RuntimeError("电子发烧友主页缺少可信文章总数")
        active = soup.select_one('.pg strong')
        page_text = active.get_text(strip=True) if active else "1"
        if not page_text.isdigit():
            raise RuntimeError("电子发烧友当前页码无效")
        return {"verified_account_id": uid, "data_source": "elecfans_public",
                "nickname": nickname.get_text(" ", strip=True),
                "total_articles": int(total_text), "current_page": int(page_text)}

    @staticmethod
    def parse_page(html, account):
        soup = BeautifulSoup(html, "html.parser")
        rows, seen = [], set()
        for item in soup.select(".article-list > li"):
            link = item.select_one('.art-list-top a[href*="/d/"]')
            if not link:
                continue
            url = urljoin("https://www.elecfans.com", link.get("href", ""))
            parsed = urlparse(url)
            id_match = re.fullmatch(r"/d/(\d+)\.html", parsed.path)
            if parsed.hostname != "www.elecfans.com" or parsed.scheme != "https":
                raise RuntimeError("电子发烧友文章链接指向非平台域名")
            if not id_match:
                raise RuntimeError("电子发烧友文章链接缺少稳定 ID")
            if id_match.group(1) in seen:
                raise RuntimeError("电子发烧友同页出现重复文章 ID")
            seen.add(id_match.group(1))
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
                "data_source": "elecfans_public",
                "verified_owner_account_id": str(account.get("platform_uid") or ""),
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
        uid = str(account.get("platform_uid") or "")
        for link in soup.select('.pg a[href]'):
            target = urlparse(urljoin("https://bbs.elecfans.com/", link.get("href", "")))
            match = re.fullmatch(r"/user/" + re.escape(uid) + r"/articles/(\d+)/?", target.path)
            if target.hostname != "bbs.elecfans.com" or not match:
                raise RuntimeError("电子发烧友分页地址不属于当前作者")
            page_numbers.append(int(match.group(1)))
        for node in soup.select('.pg strong, .pg span[title]'):
            value = node.get('title') or node.get_text(strip=True)
            match = re.fullmatch(r"(?:共\s*)?(\d+)(?:\s*页)?", value)
            if match:
                page_numbers.append(int(match.group(1)))
        return rows, max(page_numbers, default=1)

    def collect(self, account):
        uid = account.get("platform_uid", "").strip()
        base_url = account.get("profile_url") or f"https://bbs.elecfans.com/user/{uid}/articles/"
        if not uid:
            raise RuntimeError("缺少电子发烧友用户 ID")
        configured = urlparse(base_url)
        if configured.hostname != "bbs.elecfans.com" or configured.path.rstrip("/") != f"/user/{uid}/articles":
            raise RuntimeError("电子发烧友配置主页与用户 ID 不一致")
        entry = base_account(account)
        articles, seen = [], set()
        first = self.http.get(base_url, retries=1, tag=f"elecfans_{uid}_1")
        identity = self.page_identity(first.text, account)
        entry.update({k: v for k, v in identity.items() if k != "current_page"})
        if identity["current_page"] != 1:
            raise RuntimeError("电子发烧友首页返回了其他页码")
        first_rows, discovered_last_page = self.parse_page(first.text, account)
        if not first_rows and identity["total_articles"]:
            raise RuntimeError("电子发烧友首页为空但声明有文章，暂停替换")
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
                response = self.http.get(page_url, retries=1, tag=f"elecfans_{uid}_{page}")
                current_identity = self.page_identity(response.text, account)
                if current_identity["current_page"] != page or current_identity["total_articles"] != identity["total_articles"]:
                    raise RuntimeError("电子发烧友页码或文章总数在分页期间变化")
                rows, current_last_page = self.parse_page(response.text, account)
                if current_last_page != discovered_last_page:
                    raise RuntimeError("电子发烧友总页数在分页期间变化")
                if not rows:
                    raise RuntimeError("电子发烧友分页提前为空，未证明完整覆盖")
                new_rows = [row for row in rows if row["article_id"] not in seen]
                if not new_rows:
                    raise RuntimeError("电子发烧友分页重复，没有新增文章")
                for article in new_rows:
                    seen.add(article["article_id"])
                    articles.append(article)
                pages_collected = page
            except Exception as exc:
                page_error = f"第 {page} 页采集失败：{compact_error(exc)}"
                break
        if not articles and identity["total_articles"]:
            raise RuntimeError("电子发烧友公开主页未解析到文章")
        if len(articles) > identity["total_articles"]:
            raise RuntimeError("电子发烧友文章数量超过主页声明总数")
        if not page_error and not page_capped and len(articles) != identity["total_articles"]:
            page_error = "电子发烧友已到尾页，但文章数量与主页声明总数不符"
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
        seen_cursors = set()
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
                if (not isinstance(payload.get("list"), list)
                        or type(payload.get("hasMore")) not in (bool, int)
                        or payload["hasMore"] not in (False, True, 0, 1)):
                    raise RuntimeError("百家号文章列表或分页结束标志缺失，不能判定采集完整")
                items = payload["list"]
                before_count = len(articles)
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
                if items and len(articles) == before_count:
                    raise RuntimeError("百家号文章分页重复或没有可识别的新文章")
                if not has_more:
                    break
                if not items or not next_cursor or str(next_cursor) in seen_cursors:
                    raise RuntimeError("百家号仍有下一页但列表为空或分页游标缺失、重复")
                seen_cursors.add(str(next_cursor))
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


class SohuCollector:
    def __init__(self, public_client, max_pages=80):
        self.http = public_client
        self.max_pages = max(1, int(max_pages))
        self.timeout_ms = 25000

    @staticmethod
    def _state(page, uid):
        from providers.base import identifier, number
        state = page.evaluate("() => ({blocks:window.blockRenderData,request:window.originalRequest})")
        if identifier(dig(state, "request.mkey.mkey")) != uid:
            raise RuntimeError("搜狐页面媒体 ID 与配置不一致")
        blocks = state.get("blocks") or {}
        profiles = [b.get("param", {}).get("data", {}).get("list") for b in blocks.values()
                    if b.get("comp", {}).get("compName") == "BriefIntroductionCard"]
        feeds = [b.get("param", {}).get("data2") for b in blocks.values()
                 if b.get("comp", {}).get("compName") == "FeedSlideloadAuthor"]
        if len(profiles) != 1 or not isinstance(profiles[0], list) or len(profiles[0]) != 1 or len(feeds) != 1:
            raise RuntimeError("搜狐账号资料或作品流未唯一定位")
        profile, feed = profiles[0][0], feeds[0]
        if identifier(profile.get("id")) != uid:
            raise RuntimeError("搜狐资料中的作者 ID 与配置不一致")
        total = number(profile.get("column_5_text"))
        if total is None or not isinstance(feed, dict) or not isinstance(feed.get("list"), list):
            raise RuntimeError("搜狐作品总数或首屏列表缺失")
        recipe = feed.get("reqParam") or {}
        if str(dig(recipe, "content.productId")) != "325" or number(dig(recipe, "content.size")) != 20 or not recipe.get("tplCompKey"):
            raise RuntimeError("搜狐作品流分页结构发生变化")
        return profile, feed["list"], recipe["tplCompKey"], total

    @staticmethod
    def _article(raw, account):
        from providers.base import identifier, number
        uid = str(account["platform_uid"])
        url = urljoin("https://m.sohu.com", raw.get("url") or "")
        parsed = urlparse(url)
        match = re.fullmatch(r"/a/(\d+)_(\d+)", parsed.path)
        if (parsed.scheme != "https" or parsed.netloc != "m.sohu.com" or not match
                or match.group(2) != uid or identifier(raw.get("id")) != match.group(1)
                or (raw.get("authorId") is not None and identifier(raw["authorId"]) != uid)):
            raise RuntimeError("搜狐文章 ID 或精确作者 ID 不匹配")
        if str(raw.get("resourceType")) != "1" or not isinstance(raw.get("title"), str):
            raise RuntimeError("搜狐作品流出现未支持的内容类型")
        info = {part.get("image"): part.get("text") for part in raw.get("extraInfoList", []) if isinstance(part, dict)}
        def metric(key, suffix):
            value = info.get(key)
            if not isinstance(value, str) or not value.endswith(suffix):
                return None
            return number(value[:-len(suffix)])  # Rounded 万/亿 values are unknown, not exact counters.
        cover = raw.get("cover") or []
        return attach_account({"article_id": match.group(1), "title": raw["title"],
            "cover": cover[0] if isinstance(cover, list) and cover else "",
            "url": "https://m.sohu.com" + parsed.path,
            "published_at": relative_time_to_iso(info.get("time") or ""),
            "summary": raw.get("brief") or "", "tags": [], "source_author_id": uid,
            "data_source": "sohu_public", "fetched_at": datetime.now(CN_TZ).isoformat(),
            "stats": {"read": metric("pv", "阅读"), "comment": metric("comment", "评论"),
                      "like": None, "share": None, "collect": None}}, account)

    def _collect_page(self, page, account):
        from providers.base import number
        uid = str(account.get("platform_uid") or "").strip()
        url = account.get("profile_url") or f"https://m.sohu.com/media/{uid}"
        expected_url = urlparse(url)
        if not uid.isdigit() or expected_url.scheme != "https" or expected_url.netloc != "m.sohu.com" or expected_url.path != "/media/" + uid:
            raise RuntimeError("搜狐主页地址与配置媒体 ID 不匹配")
        self.http.call_count += 1
        page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        page.wait_for_function("Boolean(window.blockRenderData && window.originalRequest)", timeout=self.timeout_ms)
        profile, raw_rows, block_key, total = self._state(page, uid)
        entry = base_account(account)
        entry.update({"nickname": profile.get("title"), "verified_account_id": uid, "data_source": "sohu_public",
                      "followers": number(profile.get("column_15_text")), "lifetime_reads": number(profile.get("column_3_text")),
                      "lifetime_likes": number(profile.get("column_16_text")), "total_articles": total})
        rows, seen, pages = [], set(), 0
        for index in range(self.max_pages):
            pages += 1
            if not isinstance(raw_rows, list) or (not raw_rows and len(seen) < total):
                raise RuntimeError("搜狐空作品页未覆盖声明总数")
            for raw in raw_rows:
                row = self._article(raw, account)
                if row["article_id"] in seen:
                    raise RuntimeError("搜狐分页出现重复文章，停止覆盖")
                rows.append(row); seen.add(row["article_id"])
            if len(seen) > total:
                raise RuntimeError("搜狐文章数量超过声明总数")
            if len(seen) == total or index + 1 >= self.max_pages:
                break
            page.wait_for_function("n => document.querySelectorAll('.feed-item').length >= n", arg=len(seen), timeout=self.timeout_ms)
            def next_response(response):
                parsed = urlparse(response.url)
                if parsed.netloc != "odin.sohu.com" or parsed.path != "/odin/api/blockdata":
                    return False
                body = response.request.post_data_json
                resources = body.get("resourceList", []) if isinstance(body, dict) else []
                return any(r.get("tplCompKey") == block_key and str(dig(r, "context.mkey")) == uid
                           and number(dig(r, "content.page")) == index + 2 for r in resources)
            # Let the page construct its own public, read-only pagination POST.
            with page.expect_response(next_response, timeout=self.timeout_ms) as pending:
                page.locator(".feed-item").last.scroll_into_view_if_needed(timeout=self.timeout_ms)
                page.mouse.wheel(0, 1200)
            response = pending.value
            self.http.call_count += 1
            payload = response.json()
            if response.status != 200 or payload.get("code") != 0 or payload.get("success") is not True:
                raise RuntimeError("搜狐公开分页未成功或要求访问验证")
            raw_rows = dig(payload, "data." + block_key + ".list")
        after, _, after_key, after_total = self._state(page, uid)
        if after_key != block_key or after_total != total:
            raise RuntimeError("搜狐账号或目录总数在分页中变化")
        entry.update(covered_articles=len(rows), pages_collected=pages,
                     coverage_note=f"公开作者作品流 {len(rows)}/{total} 篇，{pages} 页；阅读、评论按公开值，其他互动未知")
        if len(rows) < total:
            entry.update(status="partial", error=f"达到最大分页数 {self.max_pages}，仍有历史文章")
        return entry, rows

    def collect(self, account):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as driver:
            try:
                browser = driver.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = driver.chromium.launch(channel="chromium", headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 950}, locale="zh-CN")
                return self._collect_page(context.new_page(), account)
            finally:
                browser.close()


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
    def _exact_count(value):
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return None
        text = str(value).strip().replace(",", "")
        return int(text) if re.fullmatch(r"\d+", text) else None

    @staticmethod
    def _profile_metrics(page):
        metrics = {}
        for text in page.locator(".relation-stat .stat-item").all_inner_texts():
            compact = re.sub(r"\s+", "", text)
            if "获赞" in compact:
                metrics["lifetime_likes"] = ToutiaoCollector._exact_count(compact.replace("获赞", ""))
            elif "粉丝" in compact:
                metrics["followers"] = ToutiaoCollector._exact_count(compact.replace("粉丝", ""))
            elif "关注" in compact:
                metrics["following"] = ToutiaoCollector._exact_count(compact.replace("关注", ""))
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
        from providers.base import identifier
        uid = str(account.get("platform_uid") or "")
        media_id = str(account.get("expected_media_id") or uid)
        author = dig(item, "itemCell.userInfo", default={})
        if (not uid or not media_id.isdigit() or not isinstance(author, dict)
                or identifier(author.get("userID")) != uid
                or identifier(author.get("mediaID")) != media_id):
            raise RuntimeError("今日头条文章精确作者 ID 与配置不符")
        article_id = identifier(item.get("group_id") or item.get("item_id") or item.get("id"))
        if not article_id:
            raise RuntimeError("今日头条文章缺少稳定 ID")
        nested_id = identifier(dig(item, "itemCell.articleBase.gidStr"))
        if not article_id.isdigit() or nested_id != article_id:
            raise RuntimeError("今日头条两组文章 ID 不一致")
        title = item.get("title") or item.get("feed_title") or ""
        if not isinstance(title, str) or not title.strip():
            raise RuntimeError("今日头条文章分类返回无标题记录，不能静默跳过")
        title = title.strip()
        counters = dig(item, "itemCell.itemCounter", default={}) or {}
        forward_info = item.get("forward_info") or {}
        def exact_metric(*values):
            known = [ToutiaoCollector._exact_count(value) for value in values]
            known = [value for value in known if value is not None]
            return max(known) if known else None
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
            "source_author_id": uid,
            "source_media_id": media_id,
            "data_source": "toutiao_public",
            "fetched_at": datetime.now(CN_TZ).isoformat(),
            "stats": {
                "read": exact_metric(counters.get("readCount"), item.get("read_count")),
                "like": exact_metric(counters.get("diggCount"), item.get("digg_count"),
                                     item.get("like_count")),
                "comment": exact_metric(counters.get("commentCount"),
                                        item.get("comment_count")),
                "share": exact_metric(counters.get("shareCount"), item.get("share_count"),
                                      forward_info.get("forward_count")),
                "collect": exact_metric(counters.get("repinCount"), item.get("repin_count")),
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
        parsed_profile = urlparse(profile_url)
        if parsed_profile.scheme != "https" or parsed_profile.netloc != "www.toutiao.com" or parsed_profile.path != f"/c/user/{uid}/":
            raise RuntimeError("今日头条作者页地址与配置 ID 不一致")
        entry["profile_url"] = profile_url
        articles, seen = [], set()
        cursors = set()
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
            else:
                raise RuntimeError("今日头条缺少文章分类入口，不能把全部内容流当文章")

            # The site normally replaces /c/user/<numeric-id>/ with an opaque
            # /c/user/token/.../ route. Bind this observed route to the exact
            # numeric userID/mediaID verified on every returned article.
            observed_profile = urlparse(page.url)
            if (observed_profile.scheme != "https" or observed_profile.netloc != "www.toutiao.com"
                    or not (observed_profile.path == f"/c/user/{uid}/"
                            or re.fullmatch(r"/c/user/token/[^/]+/", observed_profile.path))):
                raise RuntimeError("今日头条页面不是受支持的公开作者页")

            while response is not None and page_count < self.max_pages:
                self.request_count += 1
                page_count += 1
                try:
                    payload = response.json()
                except Exception as exc:
                    raise RuntimeError("今日头条文章接口未返回有效 JSON") from exc
                self._save_debug(uid, page_count, payload)
                category = dict(parse_qsl(urlparse(response.url).query)).get("category")
                if response.status != 200 or category != "pc_profile_article":
                    raise RuntimeError("今日头条响应不是已核验的公开文章列表")
                if not isinstance(payload, dict) or payload.get("message") != "success":
                    decision = payload.get("decision") if isinstance(payload, dict) else None
                    if decision:
                        raise RuntimeError("今日头条触发访问校验，请稍后重试或使用 --toutiao-headed")
                    raise RuntimeError(
                        f"今日头条文章接口异常：{compact_error(payload.get('message', payload))}")
                raw_rows, more = payload.get("data"), payload.get("has_more")
                if not isinstance(raw_rows, list) or type(more) not in (bool, int) or more not in (0, 1):
                    raise RuntimeError("今日头条文章列表或分页结束标记缺失")
                before = len(seen)
                for item in raw_rows:
                    if not isinstance(item, dict):
                        raise RuntimeError("今日头条文章记录格式改变")
                    article = self._article_from_item(item, account)
                    if not article or article["article_id"] in seen:
                        continue
                    seen.add(article["article_id"])
                    articles.append(article)
                if raw_rows and len(seen) == before:
                    raise RuntimeError("今日头条分页没有新增文章，不能视为完整")
                has_more = bool(more)
                if not has_more:
                    break
                from providers.base import identifier
                cursor = identifier(dig(payload, "next.max_behot_time"))
                if not raw_rows or not cursor.isdigit() or cursor in cursors:
                    raise RuntimeError("今日头条分页游标缺失、重复或空页仍声明更多")
                cursors.add(cursor)
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
            final_profile = urlparse(page.url)
            if (final_profile.scheme != observed_profile.scheme or final_profile.netloc != observed_profile.netloc
                    or final_profile.path != observed_profile.path):
                raise RuntimeError("今日头条作者页身份在采集期间发生变化")
            entry["verified_account_id"] = uid
            entry["data_source"] = "toutiao_public"
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


class ProviderArticleCollector:
    def __init__(self, registry, max_pages):
        self.registry, self.max_pages = registry, max_pages

    def collect(self, account):
        key = account_key(account)
        settings = self.registry.config.get("accounts", {}).get(key, {})
        try:
            result = self.registry.get(account).collect(max_pages=self.max_pages)
            # Partial results still enter the cache merge. Reject unverified
            # identities before either complete or partial records are admitted.
            verified = result.profile.get("verified_account_id")
            if not isinstance(verified, str) or not verified.strip():
                from providers.base import ProviderError
                raise ProviderError("identity_mismatch", "授权文章数据源未核验目标账号，保留原快照")
            record_verification(key, settings, result)
        except Exception as error:
            record_verification(key, settings, error=error)
            raise
        entry = base_account(account)
        entry.update(result.profile)
        entry.update({"total_articles": result.profile.get("total"),
                      "covered_articles": len(result.records), "data_source": result.source,
                      "status": "ok" if result.complete else "partial",
                      "error": "" if result.complete else result.note,
                      "coverage_note": result.note or "已完成配置范围内的后台分页"})
        return entry, [attach_account(row, account) for row in result.records]


def collect(args):
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    previous = load_previous(Path(args.out))
    run_at = datetime.now(CN_TZ).isoformat()
    public_client = HttpClient(debug=args.debug, min_interval=args.public_interval)
    api_client = ProviderRegistry()
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
        "baijiahao": ProviderArticleCollector(api_client, args.max_pages),
        "zhihu": ProviderArticleCollector(api_client, args.max_pages),
        "sohu": SohuCollector(public_client, args.max_pages),
        "xiaohongshu": ProviderArticleCollector(api_client, args.max_pages),
        "wechat_mp": ProviderArticleCollector(api_client, args.wechat_pages),
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
                if account["platform"] in PUBLIC_ARTICLE_PLATFORMS:
                    record_public_article_verification(account, entry, articles)
                    articles = annotate_public_articles(account, articles)
                articles = retain_known(articles, cached_articles, "article_id")
                articles = [{**article, "snapshot_state": "current"} for article in articles]
                restored = 0
                if entry.get("status") == "partial" and cached_articles:
                    merge_cache = cached_articles
                    if (account["platform"] in {"wechat_service", "wechat_subscription"}
                            and entry.get("data_source") == "wechat_browser"
                            and entry.get("account_key") == key
                            and entry.get("verified_account_id") == account.get("platform_uid")
                            and account.get("platform_uid")):
                        excluded_ids = {
                            str(row["article_id"]) for row in entry.get("excluded_contents", [])
                            if isinstance(row, dict) and row.get("article_id")
                            and row.get("account_key", key) == key
                            and row.get("reason") in {"deleted", "standalone_channels_video"}
                        }
                        # These rows were actually inspected and excluded. Keep
                        # unseen history, and retain the original cache for errors.
                        merge_cache = [row for row in cached_articles
                                       if str(row.get("article_id")) not in excluded_ids]
                    articles, restored = merge_records(
                        articles, merge_cache, "article_id")
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
                if account["platform"] in PUBLIC_ARTICLE_PLATFORMS:
                    record_public_article_verification(account, error=exc)
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
            finally:
                # Playwright's synchronous driver owns an event loop in this
                # thread. Release it before the next account starts its own
                # browser, including when this Toutiao account failed.
                if account.get("collector") == "toutiao":
                    toutiao.close()
            entry["request_counts"] = {
                "provider": api_client.call_count - api_before,
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
    result["provider_requests"] = api_client.call_count
    result["api_calls"] = 0
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
          f"浏览器文章请求 {toutiao.request_count} 次，平台直采请求 {api_client.call_count} 次")
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


def write_outputs(data, out_path, publish_web=True):
    atomic_write_json(out_path, data, pretty=True)
    if not publish_web:
        return
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
    parser.add_argument("--max-pages", type=int, default=500, help="单账号最大翻页数（默认500，仍受每日采集额度限制）")
    parser.add_argument("--wechat-pages", type=int, default=500,
                        help="每个公众号最多分页数；后台每页10组，官方接口每页最多20组（默认200）")
    parser.add_argument("--resolve-wechat", action="store_true",
                        help="兼容旧参数；账号身份改由登录资料核验，不再搜索")
    parser.add_argument("--wechat-stats-limit", type=int, default=0,
                        help="每个公众号补全最新 N 篇互动指标；兼容旧参数；指标由 provider 采集（默认 0）")
    parser.add_argument("--toutiao-pages", type=int, default=200,
                        help="每个今日头条账号最多采集页数，每页通常 20 篇（默认200）")
    parser.add_argument("--toutiao-timeout", type=float, default=30,
                        help="今日头条页面及分页等待超时秒数（默认 30）")
    parser.add_argument("--toutiao-headed", action="store_true",
                        help="显示今日头条采集浏览器，用于验证码或访问校验排查")
    parser.add_argument("--manual-input", default=str(MANUAL_PATH), help="人工导入 JSON 路径")
    parser.add_argument("--no-publish-web", action="store_true")
    parser.add_argument("--out", default=str(OUT_PATH), help="主输出 JSON 路径")
    parser.add_argument(
        "--only", action="append",
        help="仅刷新指定平台、账号名或 account_key；可重复使用，其他账号保留现有快照")
    args = parser.parse_args()
    load_dotenv()
    data = finalize_snapshot(make_mock(), "article") if args.mock else collect(args)
    write_outputs(data, Path(args.out), not args.no_publish_web)
    if not args.mock and any(a.get("refreshed_in_run") is not False and a.get("status") != "ok"
                             or a.get("last_attempt_at") == data.get("updated_at") and a.get("status") != "ok"
                             for a in data.get("accounts", [])):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
