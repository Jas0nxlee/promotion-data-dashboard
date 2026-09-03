#!/usr/bin/env python3
"""检查当前两套大屏快照是否满足主键、引用、数值和时间规则。"""

import argparse
import json
import sys
from pathlib import Path

from snapshot_utils import finalize_snapshot


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


def main():
    parser = argparse.ArgumentParser(description="校验视频与图文大屏数据快照")
    parser.add_argument("--video", default=str(ROOT / "data" / "dashboard_data.json"))
    parser.add_argument("--article", default=str(ROOT / "data" / "article_dashboard_data.json"))
    args = parser.parse_args()
    failures = []
    for path, kind in ((Path(args.video), "video"), (Path(args.article), "article")):
        _, errors = check(path, kind)
        failures.extend(f"{path.name}: {error}" for error in errors)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
