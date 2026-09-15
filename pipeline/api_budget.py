#!/usr/bin/env python3
"""TikHub 请求记账与每日硬上限。"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from snapshot_utils import atomic_write_json


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER_PATH = ROOT / "data" / "api_usage.json"
CN_TZ = timezone(timedelta(hours=8))


class ApiBudgetExceeded(RuntimeError):
    """达到 TikHub 每日调用上限。"""


class ApiBudget:
    def __init__(self, *, path: Path | None = None, daily_limit: int | None = None,
                 default_task: str = "unknown", now_fn=None):
        self.path = Path(path or DEFAULT_LEDGER_PATH)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        configured = os.environ.get("TIKHUB_DAILY_CALL_LIMIT", "800")
        self.daily_limit = int(configured if daily_limit is None else daily_limit)
        self.default_task = default_task
        self.now_fn = now_fn or (lambda: datetime.now(CN_TZ))

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _day(self, payload: dict, now: datetime) -> dict:
        payload["version"] = 1
        payload["timezone"] = "Asia/Shanghai"
        payload.setdefault("tracking_started_at", now.astimezone(CN_TZ).isoformat())
        days = payload.setdefault("days", {})
        day_key = now.astimezone(CN_TZ).strftime("%Y-%m-%d")
        day = days.setdefault(day_key, {
            "date": day_key,
            "limit": self.daily_limit,
            "used": 0,
            "blocked_attempts": 0,
            "by_task": {},
            "by_endpoint": {},
            "updated_at": now.astimezone(CN_TZ).isoformat(),
        })
        day["limit"] = self.daily_limit
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
            if self.daily_limit > 0 and used >= self.daily_limit:
                day["blocked_attempts"] = int(day.get("blocked_attempts") or 0) + 1
                day["updated_at"] = now.isoformat()
                atomic_write_json(self.path, payload, pretty=True)
                raise ApiBudgetExceeded(
                    f"TikHub 今日调用已达上限 {self.daily_limit} 次")

            task_name = task or self.default_task
            day["used"] = used + 1
            day["remaining"] = (
                max(0, self.daily_limit - day["used"])
                if self.daily_limit > 0 else None
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
        day["remaining"] = (
            max(0, self.daily_limit - used) if self.daily_limit > 0 else None
        )
        return {
            "date": day["date"],
            "limit": self.daily_limit,
            "used": used,
            "remaining": day["remaining"],
            "blocked_attempts": int(day.get("blocked_attempts") or 0),
            "by_task": dict(day.get("by_task") or {}),
            "tracking_started_at": payload.get("tracking_started_at"),
            "history": [dict(payload["days"][key]) for key in sorted(payload.get("days", {}))],
        }

    def remaining(self) -> int | None:
        return self.snapshot()["remaining"]
