#!/usr/bin/env python3
"""平台采集操作记账，可选每日总量上限（0为不设上限）。"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from snapshot_utils import atomic_write_json


from runtime import DATA, WEB
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER_PATH = DATA / "collection_usage.json"
CN_TZ = timezone(timedelta(hours=8))


class ApiBudgetExceeded(RuntimeError):
    """达到 平台数据采集 每日调用上限。"""


class ApiBudget:
    def __init__(self, *, path: Path | None = None, daily_limit: int | None = None,
                 default_task: str = "unknown", now_fn=None):
        self.path = Path(path or DEFAULT_LEDGER_PATH)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        configured = os.environ.get("DATA_DAILY_REQUEST_LIMIT", "0")
        self.daily_limit = int(configured if daily_limit is None else daily_limit)
        self.use_dated_overrides = daily_limit is None
        self.default_task = default_task
        self.now_fn = now_fn or (lambda: datetime.now(CN_TZ))

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _limit(self, now: datetime) -> int:
        if self.daily_limit <= 0 or not self.use_dated_overrides:
            return self.daily_limit
        path = self.path.with_name("collection_budget_overrides.json")
        try:
            overrides = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self.daily_limit
        if not isinstance(overrides, dict):
            raise ValueError("按日采集额度配置必须是日期到正整数的映射")
        key = now.astimezone(CN_TZ).strftime("%Y-%m-%d")
        if key not in overrides:
            return self.daily_limit
        value = overrides[key]
        if type(value) is not int or value <= 0:
            raise ValueError("临时采集额度必须是正整数，不能关闭额度限制")
        return value

    def _day(self, payload: dict, now: datetime) -> dict:
        limit = self._limit(now)
        payload["version"] = 1
        payload["timezone"] = "Asia/Shanghai"
        payload.setdefault("tracking_started_at", now.astimezone(CN_TZ).isoformat())
        days = payload.setdefault("days", {})
        day_key = now.astimezone(CN_TZ).strftime("%Y-%m-%d")
        day = days.setdefault(day_key, {
            "date": day_key,
            "limit": limit,
            "used": 0,
            "blocked_attempts": 0,
            "by_task": {},
            "by_endpoint": {},
            "updated_at": now.astimezone(CN_TZ).isoformat(),
        })
        day["limit"] = limit
        for stale_key in sorted(days)[:-35]:
            del days[stale_key]
        payload["current_date"] = day_key
        return day

    def consume(self, endpoint: str, *, task: str | None = None) -> dict:
        """在真实 HTTP 请求前原子扣减一次预算。"""
        now = self.now_fn().astimezone(CN_TZ)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            payload = self._load()
            day = self._day(payload, now)
            used = int(day.get("used") or 0)
            limit = day["limit"]
            if limit > 0 and used >= limit:
                day["blocked_attempts"] = int(day.get("blocked_attempts") or 0) + 1
                day["updated_at"] = now.isoformat()
                atomic_write_json(self.path, payload, pretty=True)
                raise ApiBudgetExceeded(
                    f"平台数据采集 今日调用已达上限 {limit} 次")

            task_name = task or self.default_task
            day["used"] = used + 1
            day["remaining"] = (
                max(0, limit - day["used"])
                if limit > 0 else None
            )
            day["by_task"][task_name] = int(day["by_task"].get(task_name) or 0) + 1
            day["by_endpoint"][endpoint] = int(day["by_endpoint"].get(endpoint) or 0) + 1
            day["updated_at"] = now.isoformat()
            atomic_write_json(self.path, payload, pretty=True)
            return dict(day)

    def snapshot(self) -> dict:
        now = self.now_fn().astimezone(CN_TZ)
        payload = self._load()
        day = self._day(payload, now)
        used = int(day.get("used") or 0)
        limit = day["limit"]
        day["remaining"] = (
            max(0, limit - used) if limit > 0 else None
        )
        return {
            "date": day["date"],
            "limit": limit,
            "used": used,
            "remaining": day["remaining"],
            "blocked_attempts": int(day.get("blocked_attempts") or 0),
            "by_task": dict(day.get("by_task") or {}),
            "tracking_started_at": payload.get("tracking_started_at"),
            "history": [dict(payload["days"][key]) for key in sorted(payload.get("days", {}))],
        }

    def remaining(self) -> int | None:
        return self.snapshot()["remaining"]
