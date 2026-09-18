"""Persisted verification evidence and deployment readiness, without credentials."""
import hashlib
import json
from datetime import datetime, timedelta
from runtime import DATA
from .base import now, CN_TZ
from .credentials import private_json
from .authorization import AuthorizationStore, AUTHENTICATION_ERRORS


def _has_authorization(settings):
    return not str(settings.get("provider", "")).endswith("_public")


def fingerprint(settings):
    data = {k: v for k, v in settings.items() if k not in {"last_verification", "cdp_url", "headed"}}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _previous_or_error(key, error, directory):
    path = (directory or DATA / "verification") / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        previous = {}
    return previous or {"account_key": key, "success": False, "reason": getattr(error, "reason", "error")}


def record_verification(key, settings, result=None, error=None, directory=None):
    if error is not None and (getattr(error, "authorization_guard", False) is True
                             or getattr(error, "reason", None) in {"authorization_in_progress", "session_busy"}):
        return _previous_or_error(key, error, directory)
    if (_has_authorization(settings) and error is not None
            and getattr(error, "reason", None) in AUTHENTICATION_ERRORS):
        # Serialize the generation check and evidence write with promotion.
        # Successful verification is also called from inside promotion's lock,
        # so only authentication-error handling acquires this extra lock.
        from .browser import account_lock
        from .base import ProviderError
        try:
            with account_lock(key):
                authorization = AuthorizationStore()
                if getattr(error, "authorization_error_recorded", False) is True:
                    state = authorization.read(key)
                    if (state.get("operation_id") != getattr(error, "authorization_operation_id", None)
                            or state.get("status") == "authorized"):
                        return _previous_or_error(key, error, directory)
                else:
                    state = authorization.require_reauthorization(key, error.reason, str(error))
                    error.authorization_error_recorded = True
                    error.authorization_operation_id = state.get("operation_id")
                return _record_verification(key, settings, result, error, directory)
        except ProviderError as exc:
            if exc.reason not in {"session_busy", "authorization_in_progress"}:
                raise
            # An authorization/collector owns the lock; never let a late failure
            # race its evidence commit or invalidate the session it is saving.
            return _previous_or_error(key, error, directory)
    return _record_verification(key, settings, result, error, directory)


def _record_verification(key, settings, result=None, error=None, directory=None):
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
    defaults = (["read", "like", "comment"] if settings.get("provider") == "baijiahao_creator" else
                ["play", "like", "comment", "share"] if settings.get("provider") == "wechat_channels_creator" else
                [] if settings.get("provider") == "wechat_browser" else
                ["like", "comment"] if platform in {"bilibili", "douyin", "xiaohongshu", "zhihu"} else [])
    required = settings.get("required_metrics", defaults)
    required_extra = settings.get("required_extra_metrics", ["read_users", "like_users", "share_users"]
                                  if settings.get("provider") == "wechat_browser" else [])
    # Publication pages do not return comment counts for every historical post.
    # Keep their coverage visible, but missing optional counts must not reject an
    # otherwise verified login. Explicit required_metrics still takes precedence.
    optional = ["comment"] if settings.get("provider") == "wechat_browser" else []
    records = result.records if result else []
    coverage = {metric: sum(row.get("stats", {}).get(metric) is not None for row in records) / len(records)
                if records else 1.0 for metric in dict.fromkeys([*required, *optional])}
    coverage.update({"extra_metrics." + metric: sum(row.get("extra_metrics", {}).get(metric) is not None
                                                    for row in records) / len(records) if records else 1.0
                     for metric in required_extra})
    value["metric_coverage"] = coverage
    required_keys = [*required, *("extra_metrics." + metric for metric in required_extra)]
    value["required_metric_keys"] = required_keys
    value["optional_metric_keys"] = [metric for metric in optional if metric not in required]
    value["metrics_verified"] = bool(result) and all(coverage[metric] >= 0.95 for metric in required_keys)
    if previous.get("config_fingerprint") == value["config_fingerprint"]:
        for field in ("comments_verified", "replies_verified", "comments_checked_at", "sample_comment_count",
                      "reply_verification_mode", "reply_verification_note", "sample_expected_replies",
                      "sample_comments_complete", "sample_replies_complete"):
            if field in previous:
                value[field] = previous[field]
    private_json(path, value)
    return value


def read_verification(key, settings, directory=None):
    authorization = AuthorizationStore().read(key) if _has_authorization(settings) else None
    authorization_fields = ({"authorization_status": authorization["status"]}
                            if authorization is not None else {})
    authorization_pending = (authorization is not None
                             and authorization["status"] in {"authorizing", "reauth_required"})
    folder = directory or DATA / "verification"
    path = folder / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"status": "unverified", "ready": False, **authorization_fields}
    current = value.get("config_fingerprint") == fingerprint(settings)
    try:
        fresh = datetime.fromisoformat(value["checked_at"]) > datetime.now(CN_TZ) - timedelta(hours=24)
    except (ValueError, KeyError, TypeError):
        fresh = False
    identity = value.get("identity_verified", False)
    ready = bool(not authorization_pending and current and fresh and value.get("success")
                 and identity and value.get("contents_complete") and value.get("metrics_verified"))
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
            "evidence_current": current and fresh, **authorization_fields}


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
