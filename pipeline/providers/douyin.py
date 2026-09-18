"""Read-only creator endpoints observed on the authorized Douyin website."""
import json
import time
from urllib.parse import urlsplit, parse_qs
from .base import Collection, ProviderError, identifier, number, timestamp, now
from .browser import BrowserSource, allowed
from api_budget import ApiBudgetExceeded

HOST = "https://creator.douyin.com"
PROFILE = "/web/api/media/user/info/"
WORKS = "/janus/douyin/creator/pc/work_list"
COMMENTS = "/aweme/v1/web/comment/list/"
REPLIES = "/aweme/v1/web/comment/list/reply/"
ROOT_SELECTOR = '[data-e2e="comment-list"] [data-e2e="comment-item"]:not([data-e2e="comment-item"] [data-e2e="comment-item"])'


def comment_record(raw, cid, parent=""):
    rid = identifier(raw.get("cid"))
    root = identifier(raw.get("reply_id"))
    if not rid.isdigit() or identifier(raw.get("aweme_id")) != cid or (parent and root != parent):
        raise ProviderError("identity_mismatch", "抖音评论ID、作品或回复父级不匹配")
    if not parent and root not in ("", "0"):
        raise ProviderError("schema_changed", "一级评论响应中混入二级回复")
    user = raw.get("user") or {}
    target = identifier(raw.get("reply_to_reply_id"))
    return {"comment_id": rid, "parent_comment_id": parent,
            "reply_to_comment_id": target if target not in ("", "0") else parent,
            "content": raw.get("text") or "", "user": user.get("nickname", ""),
            "user_ids": [identifier(user[k]) for k in ("uid", "sec_uid", "unique_id") if user.get(k)],
            "created_at": timestamp(raw.get("create_time")), "like": number(raw.get("digg_count")),
            "reply_count": number(raw.get("reply_comment_total")) if not parent else 0, "source": "douyin_public_web"}


def video_record(raw, item, uid):
    cid = identifier(raw.get("aweme_id"))
    if not cid.isdigit() or not cid or identifier(raw.get("author_user_id")) != uid:
        raise ProviderError("identity_mismatch", "抖音作品缺少稳定ID或作者与登录账号不符")
    stats = raw.get("statistics") or {}
    if identifier(stats.get("aweme_id")) != cid or identifier(item.get("id")) != cid:
        raise ProviderError("identity_mismatch", "抖音作品与对应指标ID不一致")
    mapping = {"play": "play_count", "like": "digg_count", "comment": "comment_count",
               "share": "share_count", "collect": "collect_count"}
    values = {k: number(stats.get(v)) for k, v in mapping.items()}
    metrics = item.get("metrics") or {}
    # The page's two structures must agree before either supplies exact counters.
    names = {"play": "view_count", "like": "like_count", "comment": "comment_count",
             "share": "share_count", "collect": "favorite_count"}
    for key, name in names.items():
        other = number(metrics.get(name))
        if values[key] is not None and other is not None and values[key] != other:
            raise ProviderError("schema_changed", "抖音两组累计指标不一致，请重新采集")
    values.update(download=number(metrics.get("download_count")), danmaku=number(metrics.get("danmaku_count")), reply=None)
    cover = (raw.get("Cover") or {}).get("url_list") or []
    duration = number(raw.get("duration"))
    return {"video_id": cid, "native_content_id": cid, "title": raw.get("desc", ""),
            "url": f"https://www.douyin.com/video/{cid}", "cover": cover[0] if cover else "",
            "published_at": timestamp(raw.get("create_time")), "duration": round(duration / 1000) if duration is not None else None,
            "duration_ms": duration, "source_author_id": uid,
            "source_author": (raw.get("author") or {}).get("nickname", ""),
            "platform_status": raw.get("status_value"), "review_status": (item.get("review") or {}).get("status"),
            "visibility": item.get("visibility") or {}, "stats": values,
            "data_source": "douyin_creator", "fetched_at": now(),
            "metric_provenance": {k: {"source": "douyin_creator", "definition": v + "_lifetime"} for k, v in {**mapping, "download": "download_count", "danmaku": "danmaku_count"}.items()}}


class DouyinProvider:
    source = "douyin_creator"

    def __init__(self, account, settings, source=None):
        self.account, self.settings = account, settings
        self.browser = source or BrowserSource(account, settings)
        self._catalog = {}
        self._comments = {}
        self._replies = {}

    @property
    def call_count(self):
        return self.browser.call_count

    def _get(self, path, params=None):
        data = self.browser.get_json(HOST + path, params)
        if data.get("status_code") != 0 or data.get("status_msg") == "blocked":
            raise ProviderError("platform_error", "抖音后台返回非成功状态，请检查会话与权限")
        return data

    def _profile(self):
        user = self._get(PROFILE).get("user")
        if not isinstance(user, dict):
            raise ProviderError("session_expired", "抖音创作后台尚未登录")
        expected = identifier(self.account.get("platform_uid"))
        actual = identifier(user.get("unique_id") or user.get("short_id"))
        uid = identifier(user.get("uid"))
        if not expected or actual != expected or not uid or (self.settings.get("expected_uid") and uid != self.settings["expected_uid"]):
            raise ProviderError("identity_mismatch", "登录抖音号或内部用户ID与项目绑定不符")
        self.verified_profile = {"nickname": user.get("nickname", ""), "verified_account_id": actual,
                                 "official_user_id": uid, "followers": number(user.get("follower_count"))}
        return self.verified_profile

    def collect(self, max_pages=200, discovery=False):
        rows, seen, cursors, total, complete = [], set(), set(), None, False
        with self.browser.session():
            profile = self._profile()
            cursor = 0
            for index in range(1 if discovery else max_pages):
                data = self._get(WORKS, {"status": 0, "count": 12, "max_cursor": cursor, "scene": "star_atlas"})
                current = number(data.get("total"))
                if current is None or (total is not None and current != total):
                    raise ProviderError("incomplete_pagination", "抖音目录总数缺失或分页期间变化")
                total = current
                raw_rows, items, more = data.get("aweme_list"), data.get("items"), data.get("has_more")
                if total == 0:
                    raw_rows, items = raw_rows or [], items or []
                if not isinstance(raw_rows, list) or not isinstance(items, list) or not isinstance(more, bool):
                    raise ProviderError("schema_changed", "抖音目录缺少两组作品列表或结束标记")
                by_id = {identifier(i.get("id")): i for i in items}
                added = 0
                for raw in raw_rows:
                    row = video_record(raw, by_id.get(identifier(raw.get("aweme_id")), {}), profile["official_user_id"])
                    if row["video_id"] not in seen:
                        rows.append(row); seen.add(row["video_id"]); added += 1
                if not more:
                    complete = len(seen) == total
                    break
                cursor = number(data.get("max_cursor"))
                if cursor is None or cursor in cursors or not added:
                    raise ProviderError("incomplete_pagination", "抖音目录页重复、为空或缺少游标")
                cursors.add(cursor)
        self._catalog.update({r["video_id"]: r for r in rows})
        profile["total"] = total
        return Collection(profile, rows, complete, "创作后台全部作品；保留审核状态和后台返回的累计指标" if complete else "尚未覆盖作品总数，保留历史缓存", self.source, self.call_count)

    def comments(self, item, max_pages=200, include_replies=True):
        cid = identifier(item.get("content_id"))
        key = cid, max_pages, include_replies
        if key not in self._comments:
            self._ensure_owned(cid, max_pages)
            with self.browser.session():
                self._profile()
                self._comments[key] = self._scan_comments(cid, max_pages, include_replies)
        return self._comments[key]

    def replies(self, item, root_id, max_pages=200):
        cid = identifier(item.get("content_id"))
        key = cid, root_id, max_pages
        if key in self._replies:
            return self._replies[key], 0
        self._ensure_owned(cid, max_pages)
        with self.browser.session():
            self._profile()
            rows, stats = self._scan_comments(cid, max_pages, True, root_id)
        return [r for r in rows if r["parent_comment_id"] == root_id], stats["root_pages"] + stats["reply_pages"]

    def _ensure_owned(self, cid, max_pages):
        if not cid.isdigit():
            raise ProviderError("identity_mismatch", "抖音评论查询需要稳定数字作品ID")
        if cid not in self._catalog:
            self.collect(max_pages=max_pages)
        if cid not in self._catalog:
            raise ProviderError("coverage_limited", "目标作品未在当前账号管理清单中核验")

    def _comment_payload(self, response):
        try:
            body = response.body()
        except Exception as error:
            # Chromium occasionally evicts a completed XHR body before CDP can
            # read it. One repeat of this page-generated GET preserves the exact
            # live URL/headers in memory; it is not a retry of access denial.
            path = urlsplit(response.url).path
            if ("No resource with given identifier" not in str(error) or response.status != 200
                    or response.request.method != "GET" or not allowed(response.url, "douyin")
                    or path not in (COMMENTS, REPLIES)):
                raise
            self.browser.budget.consume("douyin:" + path, task="platform_http")
            self.browser.call_count += 1
            time.sleep(0.2)
            retry = self.browser.context.request.get(response.url, headers=response.request.headers,
                                                      timeout=20000, max_redirects=0)
            try:
                if retry.status != 200:
                    raise ProviderError("platform_error", "复核评论响应未成功，停止采集")
                body = retry.body()
            finally:
                retry.dispose()
        return json.loads(body)

    @staticmethod
    def _reach_comment_section(page, timeout):
        for attempt in range(3):
            page.locator('[data-e2e="comment-list"]').wait_for(state="attached", timeout=timeout)
            # Resolve and scroll atomically in the document. React may replace
            # the SSR node, and an empty list may have no scrollable bounding box.
            connected = page.evaluate("""() => {
                const el = document.querySelector('[data-e2e="comment-list"]');
                if (!el || !el.isConnected) return false;
                (el.firstElementChild || el).scrollIntoView({block: 'center'});
                return true;
            }""")
            if connected:
                return
            page.wait_for_timeout(250)
        raise ProviderError("schema_changed", "评论区反复重绘，无法确认当前列表")

    def _scan_comments(self, cid, max_pages, include_replies, only_root=None):
        page = self.browser.context.new_page()
        page.route("**/*", lambda route: route.abort() if route.request.resource_type == "media" else route.fallback())
        pending, failures, response_cursors = [], [], set()
        timeout = min(60000, max(1000, int(self.settings.get("timeout_ms", 25000))))

        def receive(response):
            parsed = urlsplit(response.url)
            if not allowed(response.url, "douyin") or parsed.path not in (COMMENTS, REPLIES):
                return
            if response.request.method != "GET":
                return
            query = parse_qs(parsed.query)
            requested = query.get("aweme_id") if parsed.path == COMMENTS else query.get("item_id")
            if requested != [cid]:
                failures.append(ProviderError("identity_mismatch", "网页评论请求属于其他作品")); return
            self.browser.call_count += 1
            try:
                data = self._comment_payload(response)
                if response.status != 200 or data.get("status_code") != 0 or data.get("status_msg") == "blocked":
                    raise ProviderError("platform_error", "抖音网页评论请求被拒绝")
                more = data.get("has_more")
                if "comments" in data and data["comments"] is None and number(data.get("total")) == 0 and more in (0, False):
                    data["comments"] = []
                if not isinstance(data.get("comments"), list) or not isinstance(more, (bool, int)) or more not in (0, 1):
                    raise ProviderError("schema_changed", "评论列表或结束标记缺失，不能视为空评论")
                if number(data.get("total")) is None:
                    raise ProviderError("schema_changed", "评论响应缺少准确总数")
                request_cursor = (query.get("cursor") or [None])[0]
                response_key = (parsed.path, (query.get("comment_id") or [""])[0], request_cursor)
                if request_cursor is not None:
                    if response_key in response_cursors:
                        return
                    response_cursors.add(response_key)
                pending.append((parsed.path, (query.get("comment_id") or [""])[0], data))
            except (ProviderError, ApiBudgetExceeded) as error:
                failures.append(error)
            except Exception:
                failures.append(ProviderError("schema_changed", "抖音评论响应无法解析"))

        def take(path, rid=""):
            until = time.monotonic() + timeout / 1000
            while time.monotonic() < until:
                if failures:
                    raise failures[0]
                for i, (p, root, data) in enumerate(pending):
                    if (p, root) == (path, rid):
                        pending.pop(i); return data
                page.wait_for_timeout(100)
            raise ProviderError("incomplete_pagination", "未收到抖音网页下一页评论，请检查会话或页面可见范围")

        def action(name):
            self.browser.budget.consume("douyin:" + name, task="browser_action")
            page.wait_for_timeout(300)

        page.on("response", receive)
        roots, children, cursors = {}, {}, set()
        total = None
        root_pages = reply_pages = 0
        try:
            action("comments_page")
            page.goto(f"https://www.douyin.com/video/{cid}", wait_until="domcontentloaded", timeout=timeout)
            # Some videos fetch comments only when the comment section is reached.
            page.wait_for_timeout(300)
            if not pending and not failures:
                self._reach_comment_section(page, timeout)
            for index in range(max_pages):
                data = take(COMMENTS)
                root_pages += 1
                current = number(data["total"])
                if total is not None and current != total:
                    raise ProviderError("incomplete_pagination", "抖音评论总数在分页中变化")
                total = current
                added = 0
                for raw in data["comments"]:
                    row = comment_record(raw, cid)
                    if row["reply_count"] is None:
                        raise ProviderError("schema_changed", "一级评论缺少回复数量")
                    if row["comment_id"] not in roots:
                        roots[row["comment_id"]] = (row, raw); added += 1
                if index and data["comments"] and not added:
                    raise ProviderError("incomplete_pagination", "抖音一级评论重复页面")
                if only_root in roots or not data["has_more"]:
                    break
                cursor = number(data.get("cursor"))
                if cursor is None or cursor in cursors or not added or index + 1 >= max_pages:
                    raise ProviderError("incomplete_pagination", "一级评论未读完，游标无进展或超过上限")
                cursors.add(cursor)
                action("comments_next")
                last = page.locator(ROOT_SELECTOR).last
                last.scroll_into_view_if_needed(timeout=timeout)
                last.hover(timeout=timeout)
                page.mouse.wheel(0, 850)
            else:
                raise ProviderError("incomplete_pagination", "一级评论未完成")
            if only_root and only_root not in roots:
                raise ProviderError("coverage_limited", "目标一级评论不可见，不能将回复视为空")
            if not only_root and len(roots) + sum(r[0]["reply_count"] for r in roots.values()) != total:
                raise ProviderError("coverage_limited", "可见根评论及回复计数与平台总数不一致")
            # Work backwards so newly expanded children cannot shift earlier roots.
            for position, (rid, (row, raw)) in reversed(list(enumerate(roots.items()))):
                if only_root and rid != only_root:
                    continue
                expected = row["reply_count"]
                inline = raw.get("reply_comment") or []
                replies = {r["comment_id"]: r for r in [comment_record(x, cid, rid) for x in inline]}
                if len(replies) > expected:
                    raise ProviderError("schema_changed", "回复预览超过声明的回复数量")
                if include_replies and len(replies) < expected:
                    cursors = set()
                    for index in range(max_pages):
                        action("replies_next")
                        page.locator(ROOT_SELECTOR).nth(position).locator(".comment-reply-expand-btn span").first.click(timeout=timeout)
                        data = take(REPLIES, rid)
                        reply_pages += 1
                        before = len(replies)
                        for raw_child in data["comments"]:
                            child = comment_record(raw_child, cid, rid)
                            replies[child["comment_id"]] = child
                        if number(data["total"]) != expected:
                            raise ProviderError("incomplete_pagination", "回复总数变化，请重试")
                        if not data["has_more"]:
                            if len(replies) != expected:
                                raise ProviderError("incomplete_pagination", "二级回复未覆盖总数")
                            break
                        cursor = number(data.get("cursor"))
                        if cursor is None or cursor in cursors or len(replies) == before:
                            raise ProviderError("incomplete_pagination", "二级回复游标或记录未推进")
                        cursors.add(cursor)
                    else:
                        raise ProviderError("incomplete_pagination", "二级回复超过分页上限")
                if include_replies:
                    children.update(replies)
                if len(replies) == expected:
                    self._replies[(cid, rid, max_pages)] = list(replies.values())
            rows = [r[0] for r in roots.values()] + list(children.values())
            return rows, {"root_pages": root_pages, "reply_pages": reply_pages, "comments": len(rows),
                          "comments_complete": not bool(only_root),
                          "replies_complete": include_replies and not bool(only_root),
                          "expected_replies": sum(r[0]["reply_count"] for r in roots.values()),
                          "coverage": "public_website_visible_comments_and_replies" if include_replies else "public_website_visible_roots"}
        finally:
            page.close()
