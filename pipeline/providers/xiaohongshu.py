"""Authorized creator pages; the website generates its own signed requests."""
import copy
import re
from .base import Collection, ProviderError, identifier, number, timestamp, now, unique
from .browser import BrowserSource

HOME = "https://creator.xiaohongshu.com/new/home"
MANAGER = "https://creator.xiaohongshu.com/new/note-manager"
PROFILE = "/api/galaxy/creator/home/personal_info"
POSTED = "/api/galaxy/v2/creator/note/user/posted"


def note_record(raw):
    cid = identifier(raw.get("id"))
    if not re.fullmatch(r"[0-9a-f]{24}", cid):
        raise ProviderError("schema_changed", "小红书笔记缺少稳定的24位标识")
    images = raw.get("images_list") or []
    mapping = {"read": "view_count", "like": "likes", "comment": "comments_count",
               "share": "shared_count", "collect": "collected_count"}
    return {"article_id": cid, "native_content_id": cid, "title": raw.get("display_title", ""),
            "url": f"https://www.xiaohongshu.com/explore/{cid}",
            "cover": str(images[0].get("url", "")).replace("http://", "https://") if images else "",
            "published_at": timestamp(raw.get("time")), "publication_time_precision": "minute",
            "summary": "", "tags": [], "content_type": raw.get("type"),
            "platform_tab_status": raw.get("tab_status"),
            "stats": {k: number(raw.get(v)) for k, v in mapping.items()},
            "data_source": "xiaohongshu_creator", "fetched_at": now(),
            "metric_provenance": {k: {"source": "xiaohongshu_creator", "definition": v + "_lifetime"} for k, v in mapping.items()}}


class XiaohongshuProvider:
    source = "xiaohongshu_creator"

    def __init__(self, account, settings, source=None):
        self.account, self.settings = account, settings
        options = copy.deepcopy(settings)
        options["workflows"] = {
            "profile": {"url": HOME, "response_path": PROFILE, "rows_path": "data", "single": True},
            "contents": {"url": MANAGER, "response_path": POSTED, "rows_path": "data.notes",
                         "total_path": "data.tags.0.notes_count", "row_id_path": "id",
                         "total_first_page": True,
                         "scroll": True, "scroll_container": "div.content"},
        }
        self.browser = source or BrowserSource(account, options)
        self._notes = {}

    @property
    def call_count(self):
        return self.browser.call_count

    def _profile(self):
        expected = identifier(self.account.get("provided_id"))
        if not expected:
            raise ProviderError("setup_required", "小红书需要配置已核验的小红书号 provided_id")
        pages = self.browser.pages("profile", max_pages=1)
        if len(pages.rows) != 1 or identifier(pages.rows[0].get("red_num")) != expected:
            raise ProviderError("identity_mismatch", "小红书登录账号与项目的小红书号不一致")
        raw = pages.rows[0]
        return {"nickname": raw.get("name", ""), "followers": number(raw.get("fans_count")),
                "verified_account_id": expected}

    def collect(self, max_pages=200, discovery=False):
        with self.browser.session():
            profile = self._profile()
            pages = self.browser.pages("contents", max_pages=1 if discovery else max_pages, allow_partial=True)
        records = unique([note_record(raw) for raw in pages.rows], "article_id")
        totals = [number(tag.get("notes_count")) for p in pages.envelopes for tag in p.get("data", {}).get("tags", []) if tag.get("checked") is True]
        if not totals or None in totals or len(set(totals)) != 1:
            raise ProviderError("incomplete_pagination", "笔记总数缺失或在分页中变化，请重试")
        complete = pages.complete and len(records) == totals[0]
        # Short-lived navigation tokens stay in memory, never in snapshots/settings.
        self._notes = {raw["id"]: raw for raw in pages.rows}
        profile["total"] = totals[0]
        return Collection(profile, records, complete,
                          "创作后台全部笔记；累计指标，发布时间精度为分钟" if complete else "笔记分页尚未覆盖总数，保留历史缓存",
                          self.source, self.call_count)

    def comments(self, item, max_pages=200, include_replies=True):
        raise ProviderError("setup_required", "小红书主站评论会话及分页尚待核验")

    def replies(self, item, root_id, max_pages=200):
        raise ProviderError("setup_required", "小红书主站回复分页尚待核验")
