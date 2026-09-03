#!/usr/bin/env python3
"""采集快照的合并、质量标注与原子写入工具。"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


CN_TZ = timezone(timedelta(hours=8))
VIDEO_METRICS = ("play", "like", "comment", "share", "collect", "download")
ARTICLE_METRICS = ("read", "like", "comment", "share", "collect")


def atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件写完并 fsync 后再替换，避免页面读到半截 JSON。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, payload: dict, *, pretty: bool = False) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=1 if pretty else None),
    )


def merge_records(current: list[dict], cached: list[dict], id_field: str) -> tuple[list[dict], int]:
    """部分刷新时优先使用新记录，并补回本次未能翻到的历史缓存。"""
    merged = list(current)
    seen = {
        (str(item.get("account_key") or ""), str(item.get(id_field) or ""))
        for item in current
    }
    restored = 0
    for item in cached:
        key = (str(item.get("account_key") or ""), str(item.get(id_field) or ""))
        if not key[1] or key in seen:
            continue
        restored_item = dict(item)
        restored_item["snapshot_state"] = "cached"
        merged.append(restored_item)
        seen.add(key)
        restored += 1
    merged.sort(key=lambda item: item.get("published_at") or "", reverse=True)
    return merged, restored


def is_suspicious_drop(new_count: int, previous_count: int) -> bool:
    """防止短暂空页或页面结构变化把完整快照大幅截短。"""
    return previous_count >= 20 and new_count < max(5, int(previous_count * 0.5))


def _parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _canonical_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def _metric_coverage(records: list[dict], metrics: tuple[str, ...]) -> tuple[dict, dict]:
    def summarize(rows):
        total = len(rows)
        summary = {}
        for metric in metrics:
            values = [item.get("stats", {}).get(metric) for item in rows]
            values = [value for value in values
                      if isinstance(value, (int, float)) and not isinstance(value, bool)]
            summary[metric] = {
                "available": len(values),
                "total": total,
                "rate": round(len(values) / total, 4) if total else 0,
                "sum": int(sum(values)) if values else None,
            }
        return summary

    by_platform = {}
    for platform in sorted({item.get("platform") for item in records if item.get("platform")}):
        by_platform[platform] = summarize(
            [item for item in records if item.get("platform") == platform])
    return summarize(records), by_platform


def _video_content_key(item: dict) -> str:
    platform = str(item.get("platform") or "")
    video_id = str(item.get("video_id") or "")
    return f"{platform}:{video_id}" if video_id else f"{platform}:{_canonical_url(item.get('url'))}"


def _article_content_key(item: dict) -> str:
    # 平台文章 ID（尤其公众号 app_msg_id）不保证跨账号全局唯一。
    # 只有完全相同的公开 URL 才视为同一发布物；无 URL 时保持账号粒度。
    url = _canonical_url(item.get("url"))
    if url:
        return url
    return f"{item.get('account_key')}:{item.get('article_id')}"


def _mark_shared_videos(records: list[dict], accounts: list[dict]) -> tuple[int, int]:
    account_map = {item.get("account_key"): item for item in accounts}
    groups = defaultdict(list)
    for item in records:
        groups[_video_content_key(item)].append(item)

    shared_groups = 0
    extra_associations = 0
    for group in groups.values():
        account_keys = sorted({item.get("account_key") for item in group if item.get("account_key")})
        if len(account_keys) <= 1:
            continue
        shared_groups += 1
        extra_associations += len(account_keys) - 1

        def ownership_score(item):
            account = account_map.get(item.get("account_key"), {})
            source_id = str(item.get("source_author_id") or "")
            source_name = str(item.get("source_author") or "").replace(" ", "").casefold()
            account_uid = str(account.get("platform_uid") or "")
            account_name = str(account.get("account_name") or "").replace(" ", "").casefold()
            if source_id and account_uid and source_id == account_uid:
                return (0, str(item.get("account_key") or ""))
            if source_name and account_name and source_name == account_name:
                return (1, str(item.get("account_key") or ""))
            return (2, str(item.get("account_key") or ""))

        primary = min(group, key=ownership_score).get("account_key")
        for item in group:
            item["is_shared_content"] = True
            item["primary_account_key"] = primary
            item["associated_account_keys"] = account_keys
    return shared_groups, extra_associations


def finalize_snapshot(data: dict, kind: str) -> dict:
    """校验结构、规范异常值，并写入前端可直接使用的质量摘要。"""
    if kind not in {"video", "article"}:
        raise ValueError(f"未知快照类型: {kind}")
    records_key = "videos" if kind == "video" else "articles"
    id_field = "video_id" if kind == "video" else "article_id"
    metrics = VIDEO_METRICS if kind == "video" else ARTICLE_METRICS
    accounts = data.get("accounts") if isinstance(data.get("accounts"), list) else []
    records = data.get(records_key) if isinstance(data.get(records_key), list) else []

    account_keys = [str(item.get("account_key") or "") for item in accounts]
    duplicate_accounts = [key for key, count in Counter(account_keys).items() if key and count > 1]
    if not all(account_keys) or duplicate_accounts:
        raise ValueError(f"账号主键缺失或重复: {duplicate_accounts or '存在空主键'}")
    account_key_set = set(account_keys)

    warnings = []
    cleaned = []
    seen = set()
    future_limit = datetime.now(CN_TZ) + timedelta(days=2)
    invalid_dates = 0
    invalid_metrics = 0
    dropped_records = 0
    duplicate_records = 0
    for original in records:
        item = dict(original)
        record_id = str(item.get(id_field) or "").strip()
        key = (str(item.get("account_key") or ""), record_id)
        if key[0] not in account_key_set:
            raise ValueError(f"发现无法关联账号的记录: {key}")
        if not record_id:
            dropped_records += 1
            continue
        if key in seen:
            duplicate_records += 1
            continue
        seen.add(key)
        stats = dict(item.get("stats") or {})
        for metric in metrics:
            value = stats.get(metric)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                stats[metric] = None
                invalid_metrics += 1
        item["stats"] = stats
        if item.get("published_at"):
            parsed = _parse_datetime(item.get("published_at"))
            if parsed is None or parsed > future_limit:
                item["published_at"] = None
                invalid_dates += 1
        cleaned.append(item)

    records = cleaned
    data[records_key] = records
    if dropped_records:
        warnings.append(f"丢弃 {dropped_records} 条缺少 {id_field} 的记录")
    if duplicate_records:
        warnings.append(f"去除 {duplicate_records} 条账号内重复记录")
    if invalid_dates:
        warnings.append(f"{invalid_dates} 条发布时间无效，已按未知时间处理")
    if invalid_metrics:
        warnings.append(f"{invalid_metrics} 个指标值非法，已按未公开处理")

    if kind == "video":
        shared_content, shared_associations = _mark_shared_videos(records, accounts)
        unique_content = len({_video_content_key(item) for item in records})
    else:
        shared_groups = defaultdict(set)
        for item in records:
            shared_groups[_article_content_key(item)].add(item.get("account_key"))
        shared_content = sum(1 for values in shared_groups.values() if len(values) > 1)
        shared_associations = sum(max(0, len(values) - 1) for values in shared_groups.values())
        unique_content = len(shared_groups)

    coverage, platform_coverage = _metric_coverage(records, metrics)
    dates = sorted(parsed for parsed in (_parse_datetime(item.get("published_at")) for item in records)
                   if parsed is not None)
    status_counts = Counter(str(item.get("status") or "error") for item in accounts)
    account_issues = sum(status_counts.get(status, 0)
                         for status in ("partial", "pending", "stale", "error"))
    quality_status = "attention" if warnings or account_issues else "good"
    data["schema_version"] = 2
    data["quality"] = {
        "status": quality_status,
        "checked_at": datetime.now(CN_TZ).isoformat(),
        "record_grain": "账号与内容的关联记录" if kind == "video" else "账号下的平台发布记录",
        "association_count": len(records),
        "unique_content_count": unique_content,
        "shared_content_count": shared_content,
        "shared_association_count": shared_associations,
        "account_status_counts": dict(status_counts),
        "metric_coverage": coverage,
        "platform_metric_coverage": platform_coverage,
        "date_min": dates[0].isoformat() if dates else None,
        "date_max": dates[-1].isoformat() if dates else None,
        "warnings": warnings,
    }
    if warnings:
        data.setdefault("warnings", []).extend(
            {"scope": "quality", "message": warning} for warning in warnings)
    return data

