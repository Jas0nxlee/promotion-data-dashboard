"""Authorized creator pages; the website generates its own signed requests."""
import copy
import re
import json
import time
from urllib.parse import urlencode, urlsplit, parse_qs
from .base import Collection, ProviderError, identifier, number, timestamp, now, unique
from .browser import BrowserSource

HOME = "https://creator.xiaohongshu.com/new/home"
MANAGER = "https://creator.xiaohongshu.com/new/note-manager"
PROFILE = "/api/galaxy/creator/home/personal_info"
POSTED = "/api/galaxy/v2/creator/note/user/posted"
ME = "/api/sns/web/v2/user/me"
COMMENTS = "/api/sns/web/v2/comment/page"
REPLIES = "/api/sns/web/v2/comment/sub/page"


def comment_record(raw, note_id, parent=""):
    cid = identifier(raw.get("id"))
    if not re.fullmatch(r"[0-9a-f]{24}", cid) or raw.get("note_id") != note_id:
        raise ProviderError("identity_mismatch", "小红书评论标识缺失或属于其他笔记")
    user = raw.get("user_info") or {}
    content = raw.get("content")
    if not isinstance(content, str):
        raise ProviderError("schema_changed", "小红书评论正文格式改变")
    return {"comment_id": cid, "parent_comment_id": parent,
            "reply_to_comment_id": identifier((raw.get("target_comment") or {}).get("id")),
            "content": content, "user": user.get("nickname", ""),
            "user_ids": [identifier(user["user_id"])] if user.get("user_id") else [],
            "created_at": timestamp(raw.get("create_time")), "like": number(raw.get("like_count")),
            "reply_count": number(raw.get("sub_comment_count")) if not parent else 0,
            "source": "xiaohongshu_creator"}


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
            "public_profile": {"url": "https://www.xiaohongshu.com/explore", "response_path": ME,
                               "rows_path": "data", "single": True},
            "profile": {"url": HOME, "response_path": PROFILE, "rows_path": "data", "single": True},
            "contents": {"url": MANAGER, "response_path": POSTED, "rows_path": "data.notes",
                         "total_path": "data.tags.0.notes_count", "row_id_path": "id",
                         "total_first_page": True,
                         "scroll": True, "scroll_container": "div.content"},
        }
        self.browser = source or BrowserSource(account, options)
        self._notes = {}
        self._comment_cache = {}
        self._reply_cache = {}

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
        cid = identifier(item.get("content_id"))
        key = (cid, max_pages, include_replies)
        if key not in self._comment_cache:
            with self.browser.session():
                self._profile()
                self._public_profile()
                url = self._note_url(cid, max_pages)
                rows, stats = self._scan_comments(url, cid, max_pages, include_replies)
            self._comment_cache[key] = rows, stats
            if include_replies:
                for root in (r for r in rows if not r["parent_comment_id"]):
                    self._reply_cache[(cid, root["comment_id"], max_pages)] = [r for r in rows if r["parent_comment_id"] == root["comment_id"]]
        return self._comment_cache[key]

    def replies(self, item, root_id, max_pages=200):
        cid = identifier(item.get("content_id"))
        key = cid, root_id, max_pages
        if key in self._reply_cache:
            return self._reply_cache[key], 0
        with self.browser.session():
            self._profile()
            self._public_profile()
            rows, stats = self._scan_comments(self._note_url(cid, max_pages), cid, max_pages, True, root_id)
        selected = [r for r in rows if r["parent_comment_id"] == root_id]
        self._reply_cache[key] = selected
        return selected, stats["root_pages"] + stats["reply_pages"]

    def _public_profile(self):
        rows = self.browser.pages("public_profile", max_pages=1).rows
        if len(rows) != 1 or rows[0].get("guest") is not False:
            raise ProviderError("session_expired", "小红书主站需单独登录，创作后台会话不能替代")
        if (rows[0].get("user_id") != self.account.get("platform_uid") or
                identifier(rows[0].get("red_id")) != identifier(self.account.get("provided_id"))):
            raise ProviderError("identity_mismatch", "小红书主站与创作后台登录账号不同")
        return rows[0]

    def _note_url(self, cid, max_pages):
        if not re.fullmatch(r"[0-9a-f]{24}", cid):
            raise ProviderError("identity_mismatch", "小红书评论查询需要24位笔记ID")
        if cid not in self._notes:
            pages = self.browser.pages("contents", max_pages=max_pages, allow_partial=True)
            self._notes.update({raw["id"]: raw for raw in pages.rows})
        note = self._notes.get(cid)
        if not note or not note.get("xsec_token") or note.get("xsec_source") != "pc_creatormng":
            raise ProviderError("coverage_limited", "当前账号笔记管理中未取得目标笔记的有效导航信息")
        return f"https://www.xiaohongshu.com/explore/{cid}?" + urlencode({
            "xsec_token": note["xsec_token"], "xsec_source": "pc_creatormng"})

    def _scan_comments(self, url, cid, max_pages, include_replies, only_root=None):
        page = self.browser.context.new_page()
        pending, failures = [], []
        timeout = min(60000, max(1000, int(self.settings.get("timeout_ms", 20000))))

        def receive(response):
            parsed = urlsplit(response.url)
            if parsed.hostname != "edith.xiaohongshu.com" or parsed.path not in (COMMENTS, REPLIES):
                return
            query = parse_qs(parsed.query)
            if query.get("note_id") != [cid]:
                failures.append(ProviderError("identity_mismatch", "评论响应的笔记ID不匹配"))
                return
            self.browser.call_count += 1
            try:
                body = json.loads(response.body())
                if response.status != 200 or body.get("success") is not True or body.get("code") != 0:
                    raise ProviderError("platform_error", "小红书评论响应未成功，停止本轮")
                data = body.get("data")
                if not isinstance(data, dict) or data.get("user_id") != self.account.get("platform_uid"):
                    raise ProviderError("identity_mismatch", "评论所属笔记作者与项目账号不匹配")
                if not isinstance(data.get("comments"), list) or not isinstance(data.get("has_more"), bool):
                    raise ProviderError("schema_changed", "评论分页缺少列表或布尔结束标记")
                pending.append((parsed.path, (query.get("root_comment_id") or [""])[0], data))
            except ProviderError as error:
                failures.append(error)
            except Exception:
                failures.append(ProviderError("schema_changed", "评论响应无法解析"))

        def take(path, root=""):
            until = time.monotonic() + timeout / 1000
            while time.monotonic() < until:
                if failures:
                    raise failures[0]
                for index, (p, rid, data) in enumerate(pending):
                    if (p, rid) == (path, root):
                        pending.pop(index)
                        return data
                page.wait_for_timeout(100)
            raise ProviderError("incomplete_pagination", "未收到下一页评论响应，可能会话失效或页面改变")

        def action(name):
            self.browser.budget.consume("xiaohongshu:" + name, task="browser_action")
            page.wait_for_timeout(300)

        page.on("response", receive)
        roots, children, cursors = {}, {}, set()
        root_pages = reply_pages = 0
        try:
            action("comments")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            for index in range(max_pages):
                data = take(COMMENTS)
                root_pages += 1
                added = 0
                for raw in data["comments"]:
                    row = comment_record(raw, cid)
                    rid = row["comment_id"]
                    if rid not in roots:
                        roots[rid] = (row, raw)
                        added += 1
                if index and data["comments"] and not added:
                    raise ProviderError("incomplete_pagination", "一级评论返回重复页面")
                if only_root in roots or not data["has_more"]:
                    break
                cursor = identifier(data.get("cursor"))
                if not cursor or cursor in cursors or not added or index + 1 >= max_pages:
                    raise ProviderError("incomplete_pagination", "一级评论分页重复、空页或超过上限")
                cursors.add(cursor)
                action("comments_next")
                page.locator(".note-scroller").evaluate("el => { el.scrollTop = el.scrollHeight; }")
            else:
                raise ProviderError("incomplete_pagination", "一级评论分页未完成")
            if only_root and only_root not in roots:
                raise ProviderError("coverage_limited", "目标一级评论已不可见，不能推断回复为空")
            for rid, (row, raw) in roots.items():
                if only_root and rid != only_root:
                    continue
                inline, more = raw.get("sub_comments"), raw.get("sub_comment_has_more")
                expected = number(raw.get("sub_comment_count"))
                if not isinstance(inline, list) or not isinstance(more, bool) or expected is None:
                    raise ProviderError("schema_changed", "回复预览缺少列表、计数或结束标记")
                replies = {r["comment_id"]: r for r in [comment_record(x, cid, rid) for x in inline]}
                cursor = identifier(raw.get("sub_comment_cursor"))
                used = set()
                if include_replies:
                    for index in range(max_pages):
                        if not more:
                            break
                        if not cursor or cursor in used:
                            raise ProviderError("incomplete_pagination", "二级回复游标重复或缺失")
                        used.add(cursor)
                        action("replies_next")
                        page.locator(f"#comment-{rid}").locator("..").locator(".show-more").click(timeout=timeout)
                        data = take(REPLIES, rid)
                        reply_pages += 1
                        before = len(replies)
                        for child in data["comments"]:
                            record = comment_record(child, cid, rid)
                            replies[record["comment_id"]] = record
                        more, cursor = data["has_more"], identifier(data.get("cursor"))
                        if (more or data["comments"]) and len(replies) == before:
                            raise ProviderError("incomplete_pagination", "二级回复页面未提供新的记录")
                    if more or len(replies) != expected:
                        raise ProviderError("incomplete_pagination", "二级回复未覆盖平台计数，停止推进状态")
                    children.update(replies)
                    self._reply_cache[(cid, rid, max_pages)] = list(replies.values())
                elif not more and len(replies) == expected:
                    self._reply_cache[(cid, rid, max_pages)] = list(replies.values())
            rows = [r for r, raw in roots.values()] + list(children.values())
            if not only_root:
                if not roots:
                    # Zero-comment pages omit .total entirely. Require both the
                    # authenticated empty terminal response and the website's empty state.
                    empty_text = page.locator(".note-scroller .no-comments").inner_text(timeout=timeout)
                    if "这是一片荒地" not in empty_text:
                        raise ProviderError("coverage_limited", "空评论响应未对应页面空状态")
                else:
                    total_text = page.locator(".note-scroller .total").inner_text(timeout=timeout)
                    match = re.fullmatch(r"\s*共\s*(\d+)\s*条评论\s*", total_text)
                    covered = len(rows) if include_replies else len(roots) + sum(r[0]["reply_count"] for r in roots.values())
                    if not match or int(match[1]) != covered:
                        expected = match[1] if match else "未知"
                        raise ProviderError("coverage_limited", f"评论及回复计数{covered}与页面总数{expected}不一致，不能声明全量")
            return rows, {"root_pages": root_pages, "reply_pages": reply_pages, "comments": len(rows),
                          "coverage": "authenticated_visible_comments_and_replies" if include_replies else "authenticated_visible_roots"}
        finally:
            page.close()
