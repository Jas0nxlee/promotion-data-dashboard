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
    required = settings.get("required_metrics", ["like", "comment"] if platform in {"bilibili", "douyin", "xiaohongshu", "zhihu"} else [])
    records = result.records if result else []
    coverage = {metric: sum(row.get("stats", {}).get(metric) is not None for row in records) / len(records)
                if records else 1.0 for metric in required}
    value["metric_coverage"] = coverage
    value["metrics_verified"] = bool(result) and all(rate >= 0.95 for rate in coverage.values())
    if previous.get("config_fingerprint") == value["config_fingerprint"]:
        for field in ("comments_verified", "replies_verified", "comments_checked_at", "sample_comment_count"):
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
    complete = ready and (not details_required or value.get("comments_verified") and value.get("replies_verified"))
    return {**value, "status": "verified" if complete else "attention", "ready": bool(complete), "content_ready": ready,
            "evidence_current": current and fresh}


def record_comment_verification(key, settings, comments, directory=None):
    folder = directory or DATA / "verification"
    path = folder / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        value = {"config_fingerprint": fingerprint(settings), "success": False}
    if value.get("config_fingerprint") != fingerprint(settings):
        value = {"config_fingerprint": fingerprint(settings), "success": False}
    value.update({"comments_checked_at": now(), "comments_verified": True,
                  "replies_verified": any(x.get("parent_comment_id") for x in comments),
                  "sample_comment_count": len(comments)})
    private_json(path, value)
    return value
