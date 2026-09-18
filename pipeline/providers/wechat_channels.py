"""Authorized Channels Assistant collection using page-generated requests.

The site uses a Wujie shadow-DOM app. Page interaction retains its current
authentication/signing flow; no captured session secrets are persisted in recipes.
"""
from .base import Collection, ProviderError, identifier, number, timestamp, now, unique
from .browser import BrowserSource
import copy
import re
import time
from urllib.parse import urlparse

HOME = "https://channels.weixin.qq.com/platform"
POSTS = HOME + "/post/list"
POST_PATH = "/micro/content/cgi-bin/mmfinderassistant-bin/post/post_list"
INTERACTION = HOME + "/interaction/comment"
INTERACTION_POSTS = "/micro/interaction/cgi-bin/mmfinderassistant-bin/post/post_list"
COMMENTS = "/micro/interaction/cgi-bin/mmfinderassistant-bin/comment/comment_list"


def comment_record(raw, parent=""):
    cid = identifier(raw.get("commentId"))
    if cid in ("", "0"):
        raise ProviderError("schema_changed", "视频号评论缺少 commentId")
    user_id = identifier(raw.get("username"))
    if user_id == "0":
        user_id = ""
    content = raw.get("commentContent")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise ProviderError("schema_changed", "视频号评论正文格式发生变化")
    return {"comment_id": cid, "parent_comment_id": parent,
            "reply_to_comment_id": identifier(raw.get("replyCommentId")),
            "user": raw.get("commentNickname", ""), "user_ids": [user_id] if user_id else [],
            "author_identity_available": bool(user_id), "content": content,
            "created_at": timestamp(raw.get("commentCreatetime")), "like": number(raw.get("commentLikeCount")),
            "reply_count": 0, "source": "wechat_channels_creator"}


def video_record(raw, owner):
    cid = identifier(raw.get("objectId"))
    if not cid.startswith("export/"):
        raise ProviderError("schema_changed", "视频号作品缺少已核验的后台export标识")
    desc = raw.get("desc") or {}
    titles = desc.get("shortTitle") or []
    title = desc.get("description") or (titles[0].get("shortTitle", "") if titles else "")
    media = desc.get("media") or []
    first = media[0] if media else {}
    mapping = {"play": "readCount", "like": "likeCount", "comment": "commentCount", "share": "forwardCount"}
    return {"video_id": cid, "native_content_id": cid, "title": title,
            "url": "", "cover": first.get("coverUrl") or first.get("thumbUrl") or "",
            "published_at": timestamp(raw.get("createTime")), "duration": number(first.get("videoPlayLen")),
            "source_author": owner["nickname"], "source_author_id": owner.get("official_user_id", ""),
            "stats": {**{k: number(raw.get(v)) for k, v in mapping.items()},
                      "reply": None, "collect": None, "download": None},
            "extra_metrics": {"favCount": number(raw.get("favCount"))},
            "data_source": "wechat_channels_creator", "fetched_at": now(),
            "metric_provenance": {k: {"source": "wechat_channels_creator", "definition": v + "_lifetime"} for k, v in mapping.items()},
            "coverage_note": "后台作品标识；未将媒体临时地址当作永久链接，未把favCount推断为收藏"}


class WeChatChannelsProvider:
    source = "wechat_channels_creator"

    def __init__(self, account, settings, source=None):
        self.account, self.settings = account, settings
        self.browser = source or BrowserSource(account, copy.deepcopy(settings))
        self._comments = {}
        self._thread_seeds = {}
        self._reply_cache = {}

    @property
    def call_count(self):
        return self.browser.call_count

    def _profile(self, require_finder_id=True):
        expected = self.settings.get("expected_sph") or self.account.get("platform_uid")
        configured = self.account.get("platform_uid")
        if configured and self.settings.get("expected_sph") and configured != self.settings["expected_sph"]:
            raise ProviderError("identity_mismatch", "账号配置短号与当前绑定不同，请重新核验绑定")
        if not expected or (require_finder_id and not self.settings.get("expected_finder_id")):
            raise ProviderError("setup_required", "需要后台核验的短号和finder账号标识")
        page = self.browser.context.new_page()
        try:
            self.browser.budget.consume("wechat_channels:profile", task="browser_action")
            page.goto(HOME, wait_until="domcontentloaded", timeout=30000)
            page.bring_to_front()
            try:
                element = page.locator("#finder-uid-copy")
                element.wait_for(state="visible", timeout=20000)
            except Exception:
                if "login" in page.url:
                    raise ProviderError("session_expired", "视频号助手尚未登录") from None
                raise ProviderError("schema_changed", "首页未显示视频号稳定短号") from None
            actual = element.inner_text().strip()
            if actual != expected:
                raise ProviderError("identity_mismatch", "登录的视频号短号与绑定不同（区分大小写）")
            followers = number(page.locator(".second-info .finder-info-num").inner_text())
            homepage_total = number(page.locator(".finder-info-num").first.inner_text())
            self.verified_profile = {"nickname": self.account["account_name"], "followers": followers,
                                     "homepage_total": homepage_total,
                                     "verified_account_id": actual, "official_user_id": self.settings.get("expected_finder_id")}
            return self.verified_profile
        finally:
            page.close()

    def collect(self, max_pages=200, discovery=False):
        records, seen, total, complete = [], set(), None, False
        with self.browser.session():
            profile = self._profile()
            page, base = self._request_template()
            try:
                # Home's "视频" counter also includes image posts. Use the
                # independent video-management tab count for this video catalog.
                profile["video_catalog_total"] = self._video_catalog_total(page)
                for index in range(1, (1 if discovery else max_pages) + 1):
                    payload = self.browser.post_channels_readonly(POST_PATH, {**base,
                        "pageSize": 20, "currentPage": index, "userpageType": 11, "stickyOrder": True})
                    if payload.get("errCode") != 0 or not isinstance(payload.get("data"), dict):
                        raise ProviderError("platform_error", "视频号作品读取未成功")
                    data = payload["data"]
                    page_total = number(data.get("totalCount"))
                    if total is not None and page_total != total:
                        raise ProviderError("incomplete_pagination", "视频总数在分页期间变化，保留历史缓存")
                    total = page_total
                    rows, more = data.get("list"), data.get("continueFlag")
                    if rows is None and total == 0:
                        rows = []
                    if not isinstance(rows, list) or total is None or more not in (0, 1, False, True):
                        raise ProviderError("schema_changed", "视频号作品清单缺少列表、总数或结束标记")
                    added = 0
                    for raw in rows:
                        record = video_record(raw, profile)
                        if record["video_id"] not in seen:
                            seen.add(record["video_id"])
                            records.append(record)
                            added += 1
                    if not more:
                        complete = len(records) == total and profile["video_catalog_total"] == total
                        break
                    if not added:
                        raise ProviderError("incomplete_pagination", "视频号作品页重复或空页，尚未到达末页")
            finally:
                page.close()
        profile["total"] = total
        return Collection(profile, records, complete,
                          "视频号后台视频清单（不含图文）；部分作者身份和收藏指标可能不返回" if complete else
                          f"视频目录未完整：读取{len(records)}条，接口总数{total}，视频管理页总数{profile['video_catalog_total']}（首页含其它内容{profile.get('homepage_total')}），保留历史缓存",
                          self.source, self.call_count)

    def _video_catalog_total(self, page):
        self.browser.budget.consume("wechat_channels:video_catalog", task="browser_action")
        page.bring_to_front()
        pattern = re.compile(r"^\s*视频\s*[（(]\s*([0-9,]+)\s*[)）]\s*$")
        stage = "展开内容管理"
        try:
            # Wujie can redirect a freshly loaded deep link back to Home; use
            # the site's visible navigation after the authorized Home bootstrap.
            page.get_by_role("link", name="内容管理", exact=True).click(timeout=20000)
            stage = "进入视频管理并等待列表"
            with page.expect_response(lambda response: urlparse(response.url).path == POST_PATH
                                      and response.request.method == "POST", timeout=20000) as pending:
                page.get_by_role("link", name="视频", exact=True).click(timeout=20000)
            response = pending.value
            stage = "核验视频列表响应"
            request = response.request.post_data_json
            payload = response.json()
            scope_total = number((payload.get("data") or {}).get("totalCount"))
            if (not isinstance(request, dict) or request.get("userpageType") != 11
                    or payload.get("errCode") != 0 or scope_total is None):
                raise ProviderError("schema_changed", "视频管理页读取范围或总数发生变化")
            tab = page.get_by_text(pattern).first
            stage = "读取视频管理页计数"
            tab.wait_for(state="visible", timeout=20000)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                match = pattern.fullmatch(tab.inner_text().strip())
                if match and int(match[1].replace(",", "")) == scope_total:
                    return scope_total
                page.wait_for_timeout(100)
            raise ProviderError("incomplete_pagination", "视频管理页标签与正常列表响应的总数不一致")
        except ProviderError:
            raise
        except Exception:
            if "login" in page.url:
                raise ProviderError("session_expired", "视频管理页需要重新登录") from None
            raise ProviderError("schema_changed", f"无法核验视频管理页总数（{stage}），未使用首页合计替代") from None

    def _request_template(self, bind=False):
        page = self.browser.context.new_page()
        # Opening a comment in the UI also sends a status-update request.
        # Collection must never invoke that endpoint, nor submit a reply.
        page.route("**/comment/update_feed_comment", lambda route: route.abort())
        captured = []
        def request_seen(request):
            if urlparse(request.url).path == POST_PATH and request.method == "POST":
                body = request.post_data_json
                if isinstance(body, dict):
                    captured.append(body)
        page.on("request", request_seen)
        try:
            self.browser.budget.consume("wechat_channels:session_bootstrap", task="browser_action")
            # Fresh contexts can redirect deep links back to Home. Bootstrap on
            # Home explicitly; its preview supplies the same authorized read-session
            # fields, while our own requests set the full list's observed parameters.
            page.goto(HOME, wait_until="domcontentloaded", timeout=30000)
            page.bring_to_front()
            deadline = time.monotonic() + 30
            while not captured and time.monotonic() < deadline:
                if "login" in page.url:
                    raise ProviderError("session_expired", "视频号会话失效")
                page.wait_for_timeout(100)
            if not captured:
                raise ProviderError("schema_changed", "未捕获后台自动生成的读取会话")
            body = captured[0]
            actual_id = body.get("_log_finder_id")
            if not isinstance(actual_id, str) or not actual_id.startswith("v2_") or not actual_id.endswith("@finder"):
                raise ProviderError("schema_changed", "后台未返回可核验的finder账号标识")
            if not bind and actual_id != self.settings.get("expected_finder_id"):
                raise ProviderError("identity_mismatch", "评论会话不属于绑定视频号")
            keys = ("timestamp", "_log_finder_uin", "_log_finder_id", "rawKeyBuff", "pluginSessionId", "scene", "reqScene")
            return page, {key: body[key] for key in keys if key in body}
        except Exception:
            page.close()
            raise

    def bind_from_login(self):
        """Bind only when the configured public short ID matches on both sides of capture."""
        self.settings = copy.deepcopy(self.settings)
        if self.account.get("platform_uid"):
            self.settings["expected_sph"] = self.account["platform_uid"]
        with self.browser.session():
            before = self._profile(require_finder_id=False)
            page, base = self._request_template(bind=True)
            try:
                after = self._profile(require_finder_id=False)
                if before["verified_account_id"] != after["verified_account_id"]:
                    raise ProviderError("identity_mismatch", "绑定期间视频号身份发生变化")
                settings = copy.deepcopy(self.settings)
                settings.pop("workflows", None)
                settings.update({"provider": "wechat_channels_creator", "expected_sph": before["verified_account_id"],
                                 "expected_finder_id": base["_log_finder_id"]})
                return settings
            finally:
                page.close()

    def _more_replies(self, base, content_id, root_id, raw, max_pages):
        rows = [comment_record(child, root_id) for child in (raw.get("levelTwoComment") or [])]
        more, cursor, visited, used = raw.get("downContinueFlag"), raw.get("lastBuff"), set(), 0
        if more not in (0, 1, False, True):
            raise ProviderError("schema_changed", "回复分页结束标记缺失")
        while more:
            if used >= max_pages or not cursor or cursor in visited:
                raise ProviderError("incomplete_replies", "回复分页超限或游标重复，本轮不推进状态")
            visited.add(cursor)
            response = self.browser.post_channels_readonly(COMMENTS, {**base, "lastBuff": cursor,
                "exportId": content_id, "rootCommentId": root_id, "commentSelection": False, "forMcn": False})
            if response.get("errCode") != 0 or not isinstance(response.get("data"), dict):
                raise ProviderError("platform_error", "视频号回复读取未成功")
            data = response["data"]
            children = data.get("comment")
            more = data.get("downContinueFlag")
            if not isinstance(children, list) or more not in (0, 1, False, True):
                raise ProviderError("schema_changed", "视频号回复分页格式异常")
            if more and not children:
                raise ProviderError("incomplete_replies", "空回复页仍声明有更多数据")
            rows.extend(comment_record(child, root_id) for child in children)
            used += 1
            cursor = data.get("lastBuff")
        return unique(rows, "comment_id"), used

    def _comment_pages(self, item, max_pages, include_replies=True):
        content_id = item.get("native_content_id") or item["content_id"]
        if not content_id.startswith("export/"):
            raise ProviderError("migration_required", "旧视频号数字ID不能直接用于后台查询，请先刷新作品清单")
        cache_key = (content_id, max_pages, include_replies)
        if cache_key in self._comments:
            return self._comments[cache_key]
        comments, seen, cursor, total, reply_pages = [], set(), "", 0, 0
        truncated_replies = False
        with self.browser.session():
            self._profile()
            page, base = self._request_template()
            try:
                for index in range(max_pages):
                    if cursor in seen:
                        raise ProviderError("incomplete_pagination", "视频号评论游标重复")
                    seen.add(cursor)
                    response = self.browser.post_channels_readonly(COMMENTS, {**base, "lastBuff": cursor,
                        "exportId": content_id, "commentSelection": False, "forMcn": False})
                    if response.get("errCode") != 0 or not isinstance(response.get("data"), dict):
                        raise ProviderError("platform_error", "视频号评论读取未成功")
                    data = response["data"]
                    count = number(data.get("commentCount"))
                    roots = data.get("comment")
                    if roots is None and count == 0:
                        roots = []
                    if count is None or not isinstance(roots, list):
                        raise ProviderError("schema_changed", "视频号评论缺少列表或总数")
                    total = max(total, count)
                    for raw in roots:
                        root = comment_record(raw)
                        children = raw.get("levelTwoComment") or []
                        if not isinstance(children, list) or raw.get("downContinueFlag") not in (0, 1, False, True):
                            raise ProviderError("schema_changed", "视频号线程回复结构或分页标记缺失")
                        self._thread_seeds[(content_id, root["comment_id"], max_pages)] = raw
                        if include_replies:
                            parsed_children, used = self._more_replies(base, content_id, root["comment_id"], raw, max_pages)
                            reply_pages += used
                            self._reply_cache[(content_id, root["comment_id"], max_pages)] = parsed_children
                            root["reply_count"] = len(parsed_children)
                        else:
                            parsed_children = [comment_record(child, root["comment_id"]) for child in children]
                            lower_bound = bool(raw.get("downContinueFlag"))
                            root["reply_count"] = len(parsed_children) + int(lower_bound)
                            root["reply_count_is_lower_bound"] = lower_bound
                            truncated_replies = truncated_replies or lower_bound
                        comments.append(root)
                        comments.extend(parsed_children)
                    more = data.get("downContinueFlag")
                    if more not in (0, 1, False, True):
                        raise ProviderError("schema_changed", "评论分页标记缺失")
                    if not more:
                        comments = unique(comments, "comment_id")
                        if (include_replies or not truncated_replies) and len(comments) != total:
                            raise ProviderError("incomplete_pagination", "评论及回复数量与后台总数不一致")
                        result = (comments, index + 1, reply_pages)
                        self._comments[cache_key] = result
                        return result
                    next_cursor = data.get("lastBuff")
                    if not next_cursor or not roots:
                        raise ProviderError("incomplete_pagination", "评论未结束却缺少有效下一页")
                    cursor = next_cursor
            finally:
                page.close()
        raise ProviderError("incomplete_pagination", "视频号评论达到分页上限，本轮不推进状态")

    def comments(self, item, max_pages=200, include_replies=True):
        rows, pages, reply_pages = self._comment_pages(item, max_pages, include_replies)
        selected = rows if include_replies else [row for row in rows if not row["parent_comment_id"]]
        return selected, {"root_pages": pages, "reply_pages": reply_pages, "comments": len(selected),
                          "comments_complete": True,
                          "replies_complete": bool(include_replies),
                          "expected_replies": sum(row["reply_count"] for row in rows if not row["parent_comment_id"]),
                          "unidentified_authors": sum(not row["user_ids"] for row in selected)}

    def replies(self, item, root_id, max_pages=200):
        content_id = item.get("native_content_id") or item["content_id"]
        key = (content_id, root_id, max_pages)
        if key in self._reply_cache:
            return self._reply_cache[key], 0
        if key not in self._thread_seeds:
            self._comment_pages(item, max_pages, False)
        raw = self._thread_seeds.get(key)
        if raw is None:
            raise ProviderError("comment_missing", "当前可见评论中未找到该父评论")
        if not raw.get("downContinueFlag"):
            rows = unique([comment_record(child, root_id) for child in (raw.get("levelTwoComment") or [])], "comment_id")
            self._reply_cache[key] = rows
            return rows, 0
        with self.browser.session():
            self._profile()
            page, base = self._request_template()
            try:
                rows, used = self._more_replies(base, content_id, root_id, raw, max_pages)
            finally:
                page.close()
        self._reply_cache[key] = rows
        return rows, used
