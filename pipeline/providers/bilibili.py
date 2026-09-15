from .mapped import MappedBrowserProvider
from .http import Http
from .base import ProviderError, identifier, number, timestamp, now


class BilibiliProvider(MappedBrowserProvider):
    def __init__(self, account, settings, source=None, http=None):
        super().__init__(account, settings, source)
        self.http = http or Http({"api.bilibili.com"})

    @property
    def call_count(self):
        return self.browser.call_count + self.http.call_count

    def video_detail(self, bvid):
        result = self.http.request("GET", "https://api.bilibili.com/x/web-interface/view",
                                   params={"bvid": bvid}, headers={"Referer": "https://www.bilibili.com/"})
        if result.get("code") != 0 or not isinstance(result.get("data"), dict):
            raise ProviderError("platform_error", "视频详情不可用")
        item = result["data"]
        if item.get("bvid") != bvid:
            raise ProviderError("identity_mismatch", "返回的视频标识不匹配")
        stat, owner = item.get("stat", {}), item.get("owner", {})
        metric_map = {"play": "view", "like": "like", "coin": "coin", "collect": "favorite", "share": "share", "comment": "reply", "reply": "reply", "danmaku": "danmaku"}
        return {"video_id": bvid, "aid": identifier(item.get("aid")), "cid": identifier(item.get("cid")),
                "title": item.get("title", ""), "cover": item.get("pic", ""),
                "url": f"https://www.bilibili.com/video/{bvid}", "published_at": timestamp(item.get("pubdate")),
                "duration": number(item.get("duration")), "source_author": owner.get("name", ""),
                "source_author_id": identifier(owner.get("mid")), "data_source": "bilibili_public",
                "fetched_at": now(), "stats": {**{k: number(stat.get(v)) for k, v in metric_map.items()}, "download": None},
                "metric_provenance": {k: {"source": "bilibili_public", "definition": "platform_cumulative_count"} for k in metric_map}}

    def collect(self, max_pages=200, discovery=False):
        result = super().collect(max_pages, discovery)
        if not discovery and self.settings.get("enrich", True):
            for record in result.records:
                try:
                    detail = self.video_detail(record["video_id"])
                    for field in ("aid", "cid", "title", "cover", "url", "published_at", "duration", "source_author", "source_author_id"):
                        if record.get(field) in (None, ""):
                            record[field] = detail.get(field)
                    for metric, value in detail["stats"].items():
                        if record["stats"].get(metric) is None and value is not None:
                            record["stats"][metric] = value
                            record.setdefault("metric_provenance", {})[metric] = detail["metric_provenance"][metric]
                    record["data_sources"] = ["creator_browser", "bilibili_public"]
                except ProviderError:
                    result.complete = False
                    result.note = "部分公开视频详情未能补全；保留列表数据"
        result.request_count = self.call_count
        return result
