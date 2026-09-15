#!/usr/bin/env python3
"""检查三套大屏快照的主键、引用、数值和时间规则。"""

import argparse
import json
import sys
from pathlib import Path

from snapshot_utils import finalize_snapshot
from comment_timeline import build_public_snapshot, load_timeline


ROOT = Path(__file__).resolve().parent.parent


QUALITY_KEYS = (
    "status", "association_count", "unique_content_count",
    "shared_content_count", "shared_association_count", "account_status_counts",
    "metric_coverage", "platform_metric_coverage", "date_min", "date_max", "warnings",
)


def check(path: Path, kind: str) -> tuple[dict, list[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    stored_quality = payload.get("quality") or {}
    finalized = finalize_snapshot(payload, kind)
    quality = finalized["quality"]
    errors = []
    if stored_quality:
        mismatched = [key for key in QUALITY_KEYS
                      if stored_quality.get(key) != quality.get(key)]
        if mismatched:
            errors.append(f"已存 quality 与重新计算结果不一致: {', '.join(mismatched)}")
    if quality["warnings"]:
        errors.append("快照需要结构修复，请先重新采集或修正数据")
    print(
        f"{path.name}: {quality['status']} · 账号 {len(finalized['accounts'])} · "
        f"关联记录 {quality['association_count']} · 唯一内容 {quality['unique_content_count']}"
    )
    for metric, details in quality["metric_coverage"].items():
        print(
            f"  {metric}: {details['available']}/{details['total']} "
            f"({details['rate']:.1%})"
        )
    for warning in quality["warnings"]:
        print(f"  警告: {warning}")
    for error in errors:
        print(f"  错误: {error}")
    return quality, errors


def check_timeline(private_path: Path, public_path: Path) -> list[str]:
    if not private_path.exists():
        print(f"{private_path.name}: 待首次正式评论检查")
        return []
    private = load_timeline(private_path)
    public = json.loads(public_path.read_text(encoding="utf-8")) \
        if public_path.exists() else {}
    expected = build_public_snapshot(
        private, api_usage=public.get("api_usage"), last_scan=public.get("last_scan"))
    errors = []
    if public.get("timeline_started_at") != private.get("timeline_started_at"):
        errors.append("公开快照与私有时间线起点不一致")
    if public.get("events") != expected.get("events"):
        errors.append("公开快照事件与私有时间线不一致")
    if public.get("stats") != expected.get("stats"):
        errors.append("公开快照指标与事件重算结果不一致")
    roots = {
        (event.get("platform"), event.get("comment_id"))
        for event in private.get("events", {}).values()
        if event.get("event_type") == "comment"
    }
    for event in private.get("events", {}).values():
        if event.get("event_type") == "official_reply" and (
                event.get("platform"), event.get("parent_comment_id")) not in roots:
            errors.append(f"官方回复缺少父评论: {event.get('event_key')}")
    print(
        f"{private_path.name}: 评论 {expected['stats']['comments']} · "
        f"官方回复 {expected['stats']['official_replies']} · "
        f"待回复 {expected['stats']['pending_comments']}"
    )
    for error in errors:
        print(f"  错误: {error}")
    return errors


def main():
    parser = argparse.ArgumentParser(description="校验视频、图文与评论时间线快照")
    parser.add_argument("--video", default=str(ROOT / "data" / "dashboard_data.json"))
    parser.add_argument("--article", default=str(ROOT / "data" / "article_dashboard_data.json"))
    parser.add_argument("--timeline", default=str(ROOT / "data" / "comment_timeline.json"))
    parser.add_argument("--timeline-public", default=str(
        ROOT / "web" / "comments" / "data" / "comment_timeline.json"))
    args = parser.parse_args()
    failures = []
    for path, kind in ((Path(args.video), "video"), (Path(args.article), "article")):
        _, errors = check(path, kind)
        failures.extend(f"{path.name}: {error}" for error in errors)
    timeline_errors = check_timeline(
        Path(args.timeline), Path(args.timeline_public))
    failures.extend(f"comment_timeline.json: {error}" for error in timeline_errors)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
