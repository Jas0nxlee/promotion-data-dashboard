"""Normalized browser provider; mappings are explicit per observed workflow."""
from urllib.parse import urlparse, parse_qs
from .base import Collection, ProviderError, identifier, number, timestamp, pick, now, unique
from .browser import BrowserSource


def mapped(raw, spec):
    if isinstance(spec, list):
        return pick(raw, *spec)
    return pick(raw, spec) if isinstance(spec, str) else None


def mp_identity(url):
    query = parse_qs(urlparse(url or "").query)
    mid = (query.get("mid") or [""])[0]
    idx = (query.get("idx") or ["1"])[0]
    return f"{mid}-{idx}" if mid else ""


def normalize_record(raw, mapping, platform, kind):
    fields = mapping.get("fields", {})
    value = lambda key: mapped(raw, fields.get(key))
    content_id = identifier(value("id"))
    url = value("url") or ""
    if platform.startswith("wechat_") and platform != "wechat_channels":
        content_id = mp_identity(url) or content_id
    if not content_id:
        raise ProviderError("schema_changed", "作品记录没有稳定标识")
    id_key = "video_id" if kind == "video" else "article_id"
    metrics = ("play", "like", "comment", "reply", "danmaku", "collect", "share", "coin", "download") if kind == "video" else ("read", "like", "comment", "share", "collect")
    stats = {key: number(mapped(raw, mapping.get("stats", {}).get(key))) for key in metrics}
    record = {
        id_key: content_id, "title": value("title") or "", "url": url,
        "cover": value("cover") or "", "published_at": timestamp(value("published_at")),
        "stats": stats, "data_source": "creator_browser", "fetched_at": now(),
        "metric_provenance": {k: {"source": "creator_browser", "definition": mapping.get("definitions", {}).get(k, "unverified"),
                                  "missing_reason": "not_returned" if v is None else None}
                              for k, v in stats.items()},
    }
    # Definitions must be supplied before exact counters enter the dashboard.
    for key, val in stats.items():
        if val is not None and not mapping.get("definitions", {}).get(key):
            raise ProviderError("setup_required", f"必须先核实指标 {key} 的统计口径")
    if kind == "video":
        record.update({"aid": identifier(value("aid")), "cid": identifier(value("cid")),
                       "duration": number(value("duration")),
                       "source_author": value("source_author") or "",
                       "source_author_id": identifier(value("source_author_id"))})
    else:
        record.update({"summary": value("summary") or "", "tags": []})
    return record


def normalize_comment(raw, mapping, parent=""):
    get = lambda k: mapped(raw, mapping.get(k))
    cid = identifier(get("id"))
    if not cid:
        raise ProviderError("schema_changed", "评论缺少稳定 ID")
    count = number(get("reply_count"))
    return {"comment_id": cid, "parent_comment_id": parent or identifier(get("parent_id")),
            "content": get("content") or "", "user": get("user") or "",
            "created_at": timestamp(get("created_at")), "like_count": number(get("like")),
            "reply_count": count, "source": "creator_browser"}


class MappedBrowserProvider:
    source = "creator_browser"

    def __init__(self, account, settings, source=None):
        self.account, self.settings = account, settings
        self.browser = source or BrowserSource(account, settings)

    @property
    def call_count(self):
        return self.browser.call_count

    def _profile(self):
        config = self.settings.get("profile", {})
        expected = str(config.get("expected_id") or "")
        if not expected or not config.get("id_path"):
            raise ProviderError("setup_required", "必须配置后台身份字段及预期账号 ID")
        rows = self.browser.pages("profile", max_pages=1).rows
        if len(rows) != 1:
            raise ProviderError("identity_mismatch", "账号资料未唯一定位")
        actual = identifier(pick(rows[0], config["id_path"]))
        if actual != expected:
            raise ProviderError("identity_mismatch", "当前登录账号与绑定账号不一致，已停止采集")
        return {"nickname": pick(rows[0], config.get("name_path", "name")) or self.account["account_name"],
                "followers": number(pick(rows[0], config.get("followers_path", "followers"))),
                "verified_account_id": actual}

    def collect(self, max_pages=200, discovery=False):
        kind = "video" if self.account["platform"] in {"bilibili", "douyin", "wechat_channels"} else "article"
        mapping = self.settings.get("content_mapping")
        if not mapping:
            raise ProviderError("setup_required", "作品字段映射尚未核验")
        with self.browser.session():
            profile = self._profile()
            pages = self.browser.pages("contents", max_pages=1 if discovery else max_pages, allow_partial=True)
        records = [normalize_record(r, mapping, self.account["platform"], kind) for r in pages.rows]
        records = unique(records, "video_id" if kind == "video" else "article_id")
        profile["total"] = len(records) if pages.complete else None
        return Collection(profile, records, pages.complete, pages.note, self.source, self.call_count)

    def comments(self, item, max_pages=200, include_replies=True):
        mapping = self.settings.get("comment_mapping")
        if not mapping:
            raise ProviderError("setup_required", "评论字段映射尚未核验，不能降级为评论数")
        if self.settings.get("comment_identity_compatible") is not True:
            raise ProviderError("migration_required", "尚未验证新旧评论 ID 一致性，请先完成只读对账")
        values = {**item, "content_id": item["content_id"], "content_url": item.get("url", "")}
        with self.browser.session():
            self._profile()
            page = self.browser.pages("comments", values, max_pages=max_pages)
            roots = unique([normalize_comment(r, mapping) for r in page.rows], "comment_id")
            comments, reply_pages = list(roots), 0
            if include_replies:
                for root in roots:
                    if root["reply_count"] is None:
                        raise ProviderError("incomplete_replies", "回复数量未知，不能确认二级回复完整")
                    if not root["reply_count"]:
                        continue
                    replies = self.browser.pages("replies", {**values, "comment_id": root["comment_id"]}, max_pages=max_pages)
                    children = unique([normalize_comment(r, self.settings.get("reply_mapping", mapping), root["comment_id"])
                                       for r in replies.rows], "comment_id")
                    if len(children) < root["reply_count"]:
                        raise ProviderError("incomplete_replies", "回复记录少于声明数量，本轮不推进状态")
                    comments.extend(children)
                    reply_pages += replies.count
        return unique(comments, "comment_id"), {"root_pages": page.count, "reply_pages": reply_pages, "comments": len(comments)}
