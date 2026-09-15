"""Official published-message API; reading users are kept separate from views."""
import os
from datetime import datetime, timedelta
from .base import Collection, ProviderError, identifier, now, number, CN_TZ, unique
from .mapped import mp_identity
from .http import Http
from .credentials import WeChatToken


class WeChatOfficialProvider:
    source = "wechat_official"

    def __init__(self, account, settings, http=None):
        self.account, self.settings = account, settings
        self.http = http or Http({"api.weixin.qq.com"})
        self._verified_app_id = None

    @property
    def call_count(self):
        return self.http.call_count

    def _post(self, path, payload):
        key = f"{self.account['platform']}:{self.account['account_name']}"
        if self.settings.get("bound_account_key") != key:
            raise ProviderError("identity_mismatch", "官方令牌尚未与此公众号完成绑定核验")
        canonical = self.account.get("platform_uid")
        if not canonical or self.settings.get("bound_platform_uid") != canonical:
            raise ProviderError("identity_mismatch", "公众号原始ID尚未与此AppID完成绑定核验")
        if os.environ.get(self.settings.get("access_token_env", ""), "").strip():
            raise ProviderError("setup_required", "静态公众号令牌无法证明目标账号身份；请使用已核验绑定的AppID续期配置")
        if not self.settings.get("expected_app_id"):
            raise ProviderError("setup_required", "公众号需要已核验的AppID与原始ID绑定")
        token = WeChatToken(self.settings, self.http).get()
        response = self.http.request("POST", "https://api.weixin.qq.com" + path,
                                     params={"access_token": token}, json=payload)
        code = response.get("errcode", 0)
        if code in (40001, 40014, 42001):
            raise ProviderError("session_expired", "公众号令牌失效，请更新授权令牌")
        if code == 48001:
            raise ProviderError("permission_denied", "此账号未获得该官方 API 权限")
        if code:
            raise ProviderError("platform_error", f"公众号接口返回错误 {code}")
        self._verified_app_id = self.settings["expected_app_id"]
        return response

    def collect(self, max_pages=200, discovery=False):
        rows, visited, offset = [], set(), 0
        end = False
        for _ in range(1 if discovery else max_pages):
            response = self._post("/cgi-bin/freepublish/batchget", {"offset": offset, "count": 20, "no_content": 1})
            items = response.get("item")
            total = number(response.get("total_count"))
            if not isinstance(items, list) or total is None:
                raise ProviderError("schema_changed", "官方发布记录缺少列表或总数")
            for item in items:
                message_id = identifier(item.get("article_id"))
                if not message_id or message_id in visited:
                    raise ProviderError("incomplete_pagination", "发布消息 ID 缺失或分页重复")
                visited.add(message_id)
                parts = item.get("content", {}).get("news_item")
                if not isinstance(parts, list) or not parts:
                    raise ProviderError("schema_changed", "发布消息缺少文章/图片内容")
                for index, part in enumerate(parts, 1):
                    url = part.get("url", "")
                    stable = mp_identity(url)
                    if not stable:
                        # New identifiers must be reconciled before replacing old snapshots.
                        raise ProviderError("migration_required", "发布链接无法映射历史文章 ID，停止覆盖")
                    rows.append({"article_id": stable, "official_message_id": message_id,
                                 "title": part.get("title", ""), "url": url,
                                 "cover": part.get("thumb_url", ""), "summary": part.get("digest", ""),
                                 "published_at": None, "tags": [], "is_deleted": part.get("is_deleted", False),
                                 "stats": {k: None for k in ("read", "like", "comment", "share", "collect")},
                                 "data_source": self.source, "fetched_at": now()})
            offset += len(items)
            if offset >= total:
                end = True
                break
            if not items:
                raise ProviderError("incomplete_pagination", "发布记录空页但未达到总数")
        verified = self.settings.get("history_scope_verified") is True
        complete = end and verified
        return Collection({"nickname": self.account["account_name"], "followers": None, "total": len(rows),
                           "verified_account_id": self._verified_app_id},
                          unique(rows, "article_id"), complete,
                          "官方发布清单；未返回首次发布时间和累计互动。" + ("" if verified else "需与后台历史、图片消息对账；保留缓存。") + ("" if end else "分页尚未结束。"),
                          self.source, self.call_count)

    def daily_readers(self, day):
        date = datetime.strptime(day, "%Y-%m-%d").date()
        yesterday = datetime.now(CN_TZ).date() - timedelta(days=1)
        if date < datetime(2025, 11, 1).date() or date > yesterday:
            raise ProviderError("invalid_date", "新阅读统计只支持 2025-11-01 起至昨日的单日数据")
        response = self._post("/datacube/getarticleread", {"begin_date": day, "end_date": day})
        if not isinstance(response.get("list"), list):
            raise ProviderError("schema_changed", "阅读统计缺少列表")
        return {"metric": "daily_readers", "date": day,
                "delayed": response.get("is_delay") in (True, "true", 1),
                "items": [{"article_id": identifier(x.get("msgid")).replace("_", "-"),
                           "readers": number(x.get("detail", {}).get("read_user"))} for x in response["list"]]}
