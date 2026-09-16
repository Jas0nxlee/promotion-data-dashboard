"""Persisted verification evidence and deployment readiness, without credentials."""
import hashlib
import json
from datetime import datetime, timedelta
from runtime import DATA
from .base import now, CN_TZ
from .credentials import private_json


def fingerprint(settings):
    data = {k: v for k, v in settings.items() if k not in {"last_verification", "cdp_url", "headed"}}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def record_verification(key, settings, result=None, error=None, directory=None):
    folder = directory or DATA / "verification"
    path = folder / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        previous = {}
    value = {"account_key": key, "checked_at": now(), "config_fingerprint": fingerprint(settings),
             "success": error is None, "reason": getattr(error, "reason", "error") if error else "",
             "message": str(error) if error else "",
             "identity_verified": bool(result and result.profile.get("verified_account_id")),
             "contents_complete": bool(result and result.complete),
             "content_count": len(result.records) if result else 0,
             "comments_verified": False, "replies_verified": False}
    platform = key.split(":", 1)[0]
    defaults = (["comment"] if settings.get("provider") == "wechat_browser" else
                ["like", "comment"] if platform in {"bilibili", "douyin", "xiaohongshu", "zhihu"} else [])
    required = settings.get("required_metrics", defaults)
    required_extra = settings.get("required_extra_metrics", ["read_users", "like_users", "share_users"]
                                  if settings.get("provider") == "wechat_browser" else [])
    records = result.records if result else []
    coverage = {metric: sum(row.get("stats", {}).get(metric) is not None for row in records) / len(records)
                if records else 1.0 for metric in required}
    coverage.update({"extra_metrics." + metric: sum(row.get("extra_metrics", {}).get(metric) is not None
                                                    for row in records) / len(records) if records else 1.0
                     for metric in required_extra})
    value["metric_coverage"] = coverage
    value["metrics_verified"] = bool(result) and all(rate >= 0.95 for rate in coverage.values())
    if previous.get("config_fingerprint") == value["config_fingerprint"]:
        for field in ("comments_verified", "replies_verified", "comments_checked_at", "sample_comment_count",
                      "reply_verification_mode", "reply_verification_note", "sample_expected_replies",
                      "sample_comments_complete", "sample_replies_complete"):
            if field in previous:
                value[field] = previous[field]
    private_json(path, value)
    return value


def read_verification(key, settings, directory=None):
    folder = directory or DATA / "verification"
    path = folder / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"status": "unverified", "ready": False}
    current = value.get("config_fingerprint") == fingerprint(settings)
    try:
        fresh = datetime.fromisoformat(value["checked_at"]) > datetime.now(CN_TZ) - timedelta(hours=24)
    except (ValueError, KeyError, TypeError):
        fresh = False
    identity = value.get("identity_verified", False)
    ready = bool(current and fresh and value.get("success") and identity and value.get("contents_complete") and value.get("metrics_verified"))
    details_required = key.split(":", 1)[0] in {"bilibili", "douyin", "xiaohongshu", "wechat_channels"}
    no_replies_sample = (value.get("reply_verification_mode") == "no_replies_in_complete_sample"
                         and value.get("sample_comments_complete") is True
                         and value.get("sample_replies_complete") is True
                         and type(value.get("sample_expected_replies")) is int
                         and value["sample_expected_replies"] == 0
                         and value.get("sample_comment_count", 0) > 0)
    complete = ready and (not details_required or value.get("comments_verified")
                         and (value.get("replies_verified") or no_replies_sample))
    return {**value, "status": "verified" if complete else "attention", "ready": bool(complete), "content_ready": ready,
            "evidence_current": current and fresh}


def record_comment_verification(key, settings, comments, directory=None, *, stats=None):
    folder = directory or DATA / "verification"
    path = folder / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        value = {"config_fingerprint": fingerprint(settings), "success": False}
    if value.get("config_fingerprint") != fingerprint(settings):
        value = {"config_fingerprint": fingerprint(settings), "success": False}
    stats = stats or {}
    observed_replies = any(x.get("parent_comment_id") for x in comments)
    # A complete nonempty root sample can prove that there are no replies to
    # exercise. It cannot prove that a second-level page was ever requested.
    no_replies_sample = (bool(comments) and stats.get("comments_complete") is True
                         and stats.get("replies_complete") is True
                         and type(stats.get("expected_replies")) is int
                         and stats["expected_replies"] == 0
                         and all(x.get("parent_comment_id") == ""
                                 and type(x.get("reply_count")) is int
                                 and x["reply_count"] == 0 for x in comments))
    mode = ("observed_replies" if observed_replies else
            "no_replies_in_complete_sample" if no_replies_sample else "unverified")
    notes = {"observed_replies": "样本已读取二级回复明细",
             "no_replies_in_complete_sample": "完整样本无回复，未执行二级分页",
             "unverified": "样本尚未证明回复覆盖"}
    value.update({"comments_checked_at": now(), "comments_verified": True,
                  "replies_verified": observed_replies, "sample_comment_count": len(comments),
                  "reply_verification_mode": mode, "reply_verification_note": notes[mode],
                  "sample_comments_complete": stats.get("comments_complete") is True,
                  "sample_replies_complete": stats.get("replies_complete") is True,
                  "sample_expected_replies": stats.get("expected_replies")})
    private_json(path, value)
    return value
