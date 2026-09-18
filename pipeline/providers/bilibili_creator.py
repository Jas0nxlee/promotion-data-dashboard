"""Bilibili creator endpoints observed on the authorized website, 2026-09-15."""
from .base import Collection, ProviderError, identifier, number, timestamp, now, unique
from .browser import BrowserSource


class BilibiliCreatorProvider:
    source = "bilibili_creator"

    def __init__(self, account, settings, source=None):
        self.account, self.settings = account, settings
        self.browser = source or BrowserSource(account, settings)
        self._comment_cache = {}

    @property
    def call_count(self):
        return self.browser.call_count

    def _get(self, host, path, params=None, *, min_interval=None):
        kwargs = {"min_interval": min_interval} if min_interval is not None else {}
        payload = self.browser.get_json(f"https://{host}{path}", params, **kwargs)
        if payload.get("code") == -101:
            raise ProviderError("session_expired", "B站账号未登录")
        if path == "/x/v2/reply/up/fulllist" and payload.get("code") == -352:
            raise ProviderError("rate_limited", "B站评论接口返回业务码 -352，本轮暂停该账号")
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise ProviderError("platform_error", "B站原生接口响应未成功")
        return payload["data"]

    def _profile(self):
        profile = self._get("api.bilibili.com", "/x/web-interface/nav")
        expected = str(self.account.get("platform_uid") or self.settings.get("expected_uid") or "")
        if not expected or profile.get("isLogin") is not True or identifier(profile.get("mid")) != expected:
            raise ProviderError("identity_mismatch", "当前B站登录账号与项目UID不一致")
        return {"nickname": profile.get("uname", self.account["account_name"]),
                "verified_account_id": expected, "followers": None}

    def collect(self, max_pages=200, discovery=False):
        rows, seen, complete, total = [], set(), False, None
        with self.browser.session():
            profile = self._profile()
            fans = self._get("api.bilibili.com", "/x/relation/stat", {"vmid": profile["verified_account_id"]})
            if identifier(fans.get("mid")) != profile["verified_account_id"]:
                raise ProviderError("identity_mismatch", "粉丝数据UID不匹配")
            profile["followers"] = number(fans.get("follower"))
            for page in range(1, (1 if discovery else max_pages) + 1):
                data = self._get("member.bilibili.com", "/x/web/archives",
                                 {"status": "pubed", "pn": page, "ps": 10, "coop": 1, "interactive": 1})
                items = data.get("arc_audits")
                total = number(data.get("page", {}).get("count"))
                if items is None and total == 0:
                    items = []
                if not isinstance(items, list) or total is None:
                    raise ProviderError("schema_changed", "投稿清单缺少记录或总数")
                new_on_page = 0
                for raw in items:
                    archive, stats = raw.get("Archive", {}), raw.get("stat", {})
                    bvid = identifier(archive.get("bvid"))
                    if not bvid:
                        raise ProviderError("schema_changed", "投稿记录缺少 BV ID")
                    if bvid in seen:
                        continue
                    seen.add(bvid)
                    new_on_page += 1
                    metric_map = {"play": "view", "like": "like", "comment": "reply", "reply": "reply", "coin": "coin", "share": "share", "collect": "favorite", "danmaku": "danmaku"}
                    cid_list = raw.get("cid_list") or []
                    rows.append({"video_id": bvid, "native_content_id": bvid,
                                 "aid": identifier(archive.get("aid")), "cid": identifier(cid_list[0]) if cid_list else "",
                                 "title": archive.get("title", ""), "cover": str(archive.get("cover", "")).replace("http://", "https://"),
                                 "url": f"https://www.bilibili.com/video/{bvid}", "published_at": timestamp(archive.get("ptime")),
                                 "duration": number(archive.get("duration")),
                                 "source_author_id": identifier(archive.get("mid")), "source_author": archive.get("author") or "",
                                 "visibility": "restricted" if archive.get("no_public") or archive.get("is_only_self") else "public",
                                 "stats": {**{k: number(stats.get(v)) for k, v in metric_map.items()}, "download": None},
                                 "data_source": self.source, "fetched_at": now(),
                                 "metric_provenance": {k: {"source": self.source, "definition": "lifetime_count"} for k in metric_map}})
                if len(seen) >= total:
                    complete = True
                    break
                if not new_on_page:
                    raise ProviderError("incomplete_pagination", "投稿分页重复或空页，总数尚未覆盖")
        profile["total"] = total
        return Collection(profile, rows, complete, "已通过稿件清单" if complete else "已读取部分稿件，尚有历史分页", self.source, self.call_count)

    def _all_comments(self, item, max_pages):
        bvid = item["content_id"]
        if not bvid.startswith("BV"):
            raise ProviderError("identity_mismatch", "B站评论查询要求已归一化 BV ID")
        cache_key = (bvid, max_pages)
        if cache_key in self._comment_cache:
            return self._comment_cache[cache_key]
        rows, seen, total = [], set(), None
        with self.browser.session():
            self._profile()
            for page in range(1, max_pages + 1):
                data = self._get("api.bilibili.com", "/x/v2/reply/up/fulllist",
                                 {"order": 1, "filter": -1, "type": 1, "bvid": bvid,
                                  "pn": page, "ps": 10, "charge_plus_filter": "false"},
                                 min_interval=2.0)
                items = data.get("list")
                current_total = number(data.get("page", {}).get("total"))
                if items is None and current_total == 0:
                    items = []
                if not isinstance(items, list) or current_total is None:
                    raise ProviderError("schema_changed", "评论清单缺少记录或总数")
                if total is not None and current_total != total:
                    raise ProviderError("incomplete_pagination", "评论总数在分页期间改变，本轮不推进状态")
                total = current_total
                if total >= 50000:
                    raise ProviderError("coverage_limited", "后台仅展示最近50000条评论，不能声明全量")
                additions = 0
                for raw in items:
                    if raw.get("bvid") != bvid:
                        raise ProviderError("identity_mismatch", "评论响应包含其他视频")
                    cid = identifier(raw.get("rpid"))
                    if not cid:
                        raise ProviderError("schema_changed", "评论缺少rpid")
                    if cid in seen:
                        continue
                    seen.add(cid)
                    additions += 1
                    member = raw.get("member") or {}
                    root = identifier(raw.get("root"))
                    rows.append({"comment_id": cid, "parent_comment_id": root if root not in ("", "0") else "",
                                 "reply_to_comment_id": identifier(raw.get("parent")),
                                 "content": (raw.get("content") or {}).get("message", ""),
                                 "user": member.get("uname", ""), "user_ids": [identifier(member.get("mid"))],
                                 "created_at": timestamp(raw.get("ctime")), "like": number(raw.get("like")),
                                 "reply_count": number(raw.get("rcount")), "source": self.source})
                if len(seen) > total:
                    raise ProviderError("incomplete_pagination", "评论唯一ID数超过声明总数，本轮不推进状态")
                if len(seen) == total:
                    result = (unique(rows, "comment_id"), page)
                    self._comment_cache[cache_key] = result
                    return result
                if not additions:
                    raise ProviderError("incomplete_pagination", "评论页重复或为空，本轮不推进状态")
        raise ProviderError("incomplete_pagination", "评论超过分页上限，本轮不推进状态")

    def comments(self, item, max_pages=200, include_replies=True):
        rows, pages = self._all_comments(item, max_pages)
        selected = rows if include_replies else [r for r in rows if not r["parent_comment_id"]]
        # fulllist includes roots and replies in one creator-visible catalog.
        # _all_comments returns only after exact, stable total coverage; there is
        # no independent reply page to count. Root-only output must not certify
        # delivery of the replies that were intentionally omitted from it.
        return selected, {"root_pages": pages, "reply_pages": 0, "comments": len(selected),
                          "comments_complete": True, "replies_complete": include_replies is True,
                          "expected_replies": sum(bool(r["parent_comment_id"]) for r in rows),
                          "coverage": "creator_visible_comments_and_replies"}

    def replies(self, item, root_id, max_pages=200):
        cached = (item["content_id"], max_pages) in self._comment_cache
        rows, pages = self._all_comments(item, max_pages)
        return [r for r in rows if r["parent_comment_id"] == root_id], 0 if cached else pages
