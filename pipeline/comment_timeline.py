#!/usr/bin/env python3
"""上线后评论与官方回复时间线持久化。"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from snapshot_utils import atomic_write_json


ROOT = Path(__file__).resolve().parent.parent
TIMELINE_PATH = ROOT / "data" / "comment_timeline.json"
WEB_TIMELINE_PATH = ROOT / "web" / "comments" / "data" / "comment_timeline.json"
CN_TZ = timezone(timedelta(hours=8))


def _now_iso(now=None) -> str:
    return (now or datetime.now(CN_TZ)).astimezone(CN_TZ).isoformat()


def _parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def load_timeline(path: Path | None = None, *, now=None) -> dict:
    path = Path(path or TIMELINE_PATH)
    try:
        import json
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    value["version"] = 1
    value.setdefault("timeline_started_at", _now_iso(now))
    if not isinstance(value.get("events"), dict):
        value["events"] = {}
    return value


def event_key(platform: str, comment_id: str) -> str:
    return f"{platform}:{comment_id}"


def is_after_start(created_at, timeline: dict) -> bool:
    created = _parse_time(created_at)
    started = _parse_time(timeline.get("timeline_started_at"))
    return bool(created and started and created >= started)


def record_comment(timeline: dict, item: dict, comment: dict, *, observed_at: str) -> bool:
    """只保存上线后且具有可靠平台时间的非官方一级评论。"""
    if not is_after_start(comment.get("created_at"), timeline):
        return False
    key = event_key(item["platform"], str(comment["comment_id"]))
    events = timeline["events"]
    existing = events.get(key)
    if existing:
        existing.update({
            "content": comment.get("content") or existing.get("content", ""),
            "like": comment.get("like"),
            "reply_count": comment.get("reply_count", 0),
            "last_seen_at": observed_at,
        })
        return False
    events[key] = {
        "event_key": key,
        "event_type": "comment",
        "platform": item.get("platform", ""),
        "platform_label": item.get("platform_label", ""),
        "account_key": item.get("account_key", ""),
        "account_name": item.get("account_name", ""),
        "business_line": item.get("business_line", ""),
        "content_id": item.get("content_id", ""),
        "content_title": item.get("title", ""),
        "content_url": item.get("url", ""),
        "comment_id": str(comment.get("comment_id") or ""),
        "parent_comment_id": "",
        "author": comment.get("user") or "匿名用户",
        "content": comment.get("content") or "",
        "platform_created_at": comment.get("created_at"),
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "like": comment.get("like"),
        "reply_count": comment.get("reply_count", 0),
        "official": False,
        "time_source": "platform",
    }
    return True


def record_official_reply(timeline: dict, item: dict, root: dict, reply: dict,
                          *, observed_at: str) -> bool:
    """仅保存上线后新评论下的官方回复。"""
    if not is_after_start(reply.get("created_at"), timeline):
        return False
    root_key = event_key(item["platform"], str(root["comment_id"]))
    if root_key not in timeline["events"]:
        return False
    key = event_key(item["platform"], str(reply["comment_id"]))
    events = timeline["events"]
    existing = events.get(key)
    if existing:
        existing.update({
            "content": reply.get("content") or existing.get("content", ""),
            "like": reply.get("like"),
            "last_seen_at": observed_at,
        })
        return False
    root_time = _parse_time(timeline["events"][root_key].get("platform_created_at"))
    reply_time = _parse_time(reply.get("created_at"))
    response_seconds = None
    if root_time and reply_time and reply_time >= root_time:
        response_seconds = int((reply_time - root_time).total_seconds())
    events[key] = {
        "event_key": key,
        "event_type": "official_reply",
        "platform": item.get("platform", ""),
        "platform_label": item.get("platform_label", ""),
        "account_key": item.get("account_key", ""),
        "account_name": item.get("account_name", ""),
        "business_line": item.get("business_line", ""),
        "content_id": item.get("content_id", ""),
        "content_title": item.get("title", ""),
        "content_url": item.get("url", ""),
        "comment_id": str(reply.get("comment_id") or ""),
        "parent_comment_id": str(root.get("comment_id") or ""),
        "author": reply.get("user") or item.get("account_name") or "官方账号",
        "content": reply.get("content") or "",
        "platform_created_at": reply.get("created_at"),
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "like": reply.get("like"),
        "reply_count": 0,
        "official": True,
        "time_source": "platform",
        "response_seconds": response_seconds,
    }
    return True


def _percentile(values: list[int], percentile: float):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def build_public_snapshot(timeline: dict, *, api_usage: dict | None = None,
                          last_scan: dict | None = None, now=None) -> dict:
    events = sorted(
        (dict(event) for event in timeline.get("events", {}).values()),
        key=lambda event: event.get("platform_created_at") or event.get("first_seen_at") or "",
        reverse=True,
    )
    comments = [event for event in events if event.get("event_type") == "comment"]
    replies = [event for event in events if event.get("event_type") == "official_reply"]
    replied_ids = {event.get("parent_comment_id") for event in replies}
    first_reply = {}
    for event in replies:
        parent_id = event.get("parent_comment_id")
        seconds = event.get("response_seconds")
        if parent_id and isinstance(seconds, int):
            first_reply[parent_id] = min(seconds, first_reply.get(parent_id, seconds))
    response_values = list(first_reply.values())
    replied_comments = sum(1 for event in comments if event.get("comment_id") in replied_ids)
    stats = {
        "comments": len(comments),
        "official_replies": len(replies),
        "replied_comments": replied_comments,
        "pending_comments": max(0, len(comments) - replied_comments),
        "reply_rate": round(replied_comments / len(comments), 4) if comments else 0,
        "response_samples": len(response_values),
        "response_average_seconds": (
            round(sum(response_values) / len(response_values)) if response_values else None
        ),
        "response_median_seconds": (
            round(statistics.median(response_values)) if response_values else None
        ),
        "response_p90_seconds": _percentile(response_values, 0.9),
    }
    return {
        "schema_version": 1,
        "updated_at": _now_iso(now),
        "timeline_started_at": timeline.get("timeline_started_at"),
        "events": events,
        "stats": stats,
        "api_usage": api_usage or {},
        "last_scan": last_scan or {},
        "provenance": {
            "source": "TikHub 平台评论接口",
            "scope": "功能上线后的新增评论与已确认官方回复",
            "supported_platforms": ["抖音", "B站", "小红书", "视频号"],
            "time_policy": "仅使用平台返回的发布时间计算官方响应耗时",
            "limitations": "无平台时间戳或无法通过用户 ID 确认官方身份的记录不纳入时线",
        },
    }


def save_timeline(timeline: dict, *, api_usage: dict | None = None,
                  last_scan: dict | None = None, private_path: Path | None = None,
                  public_path: Path | None = None, now=None) -> dict:
    timeline["updated_at"] = _now_iso(now)
    snapshot = build_public_snapshot(
        timeline, api_usage=api_usage, last_scan=last_scan, now=now)
    atomic_write_json(Path(private_path or TIMELINE_PATH), timeline, pretty=True)
    atomic_write_json(Path(public_path or WEB_TIMELINE_PATH), snapshot, pretty=True)
    return snapshot
