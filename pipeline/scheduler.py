#!/usr/bin/env python3
"""容器内北京时间调度：每日数据采集，每小时评论检查与邮件发送。"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import signal
import fcntl
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from snapshot_utils import atomic_write_json


from runtime import DATA, is_test
ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = DATA / "scheduler_state.json"
CN_TZ = timezone(timedelta(hours=8))


@contextmanager
def scheduler_guard():
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "scheduler.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("该运行目录已有调度器，拒绝重复启动") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def log(message: str) -> None:
    print(f"[{now_cn().isoformat(timespec='seconds')}] {message}", flush=True)


def load_state() -> dict:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    atomic_write_json(STATE_PATH, state, pretty=True)


def command(script: str, env_name: str) -> list[str]:
    return [sys.executable, str(ROOT / "pipeline" / script),
            *shlex.split(os.environ.get(env_name, ""))]


def run(name: str, argv: list[str]) -> int:
    log(f"开始{name}：{' '.join(shlex.quote(part) for part in argv)}")
    with subprocess.Popen(argv, cwd=ROOT, start_new_session=True) as process:
        try:
            code = process.wait(timeout=int(os.environ.get("COLLECTION_TIMEOUT_SECONDS", "1800")))
        except subprocess.TimeoutExpired:
            log(f"{name}超过执行时限，终止本任务进程组")
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                pass
            return 124
    log(f"结束{name}：exit={code}")
    return code


def run_data_collection() -> dict:
    results = {
        "video": run("视频数据采集", command("fetch_data.py", "VIDEO_FETCH_ARGS")),
        "article": run("图文数据采集", command("fetch_article_data.py", "ARTICLE_FETCH_ARGS")),
    }
    results["validate"] = run(
        "快照校验", [sys.executable, str(ROOT / "pipeline" / "validate_snapshots.py")])
    return results


def run_comment_cycle() -> dict:
    monitor = run("评论检查", command("comment_monitor.py", "COMMENT_MONITOR_ARGS"))
    # 即使本轮评论接口失败，也尝试重发队列里上一轮未发送的邮件。
    mail = 0 if is_test() else run("邮件发送", [sys.executable, str(ROOT / "pipeline" / "send_comment_alerts.py")])
    return {"monitor": monitor, "mail": mail}


def mark(state: dict, key: str, slot: str, results: dict) -> None:
    state[key] = slot
    state[f"{key}_finished_at"] = now_cn().isoformat()
    state[f"{key}_results"] = results
    save_state(state)


def run_due(current: datetime | None = None) -> None:
    current = current or now_cn()
    daily_hour = int(os.environ.get("DATA_COLLECTION_HOUR", "8"))
    state = load_state()
    hour_slot = current.strftime("%Y-%m-%dT%H")
    if current.minute == 0:
        day_slot = current.strftime("%Y-%m-%d")
        if current.hour == daily_hour and state.get("last_data_slot") != day_slot:
            mark(state, "last_data_slot", day_slot, run_data_collection())
            state = load_state()
    # 容器首次启动若不在整点，也立即建立评论基线；之后每个小时
    # 只执行一次。持久化 slot 可避免同小时重启重复全量扫描。
    if state.get("last_comment_slot") != hour_slot:
        mark(state, "last_comment_slot", hour_slot, run_comment_cycle())


def scheduler_loop() -> None:
    daily_hour = int(os.environ.get("DATA_COLLECTION_HOUR", "8"))
    poll_seconds = max(10, int(os.environ.get("SCHEDULER_POLL_SECONDS", "20")))
    log(f"调度器启动：数据采集每天 {daily_hour:02d}:00，评论检查每小时整点")
    engine = DispatchScheduler()
    try:
        while True:
            engine.tick()
            time.sleep(poll_seconds)
    finally:
        engine.executor.shutdown(wait=True, cancel_futures=True)


class DispatchScheduler:
    """Separate data/comment lanes; catch up missed daily slots and retry failures."""
    def __init__(self, executor=None):
        self.executor = executor or ThreadPoolExecutor(max_workers=2)
        self.running = {}

    def tick(self, current=None):
        current = current or now_cn()
        state = load_state()
        for kind, (future, slot, started) in list(self.running.items()):
            if not future.done():
                continue
            try:
                results = future.result()
            except Exception:
                results = {"worker": 1}
            success = not any(results.values())
            state[f"last_{kind}_attempt_at"] = current.isoformat()
            state[f"last_{kind}_results"] = results
            if success:
                state[f"last_{kind}_slot"] = slot
            else:
                state[f"{kind}_retry_after"] = (current + timedelta(minutes=15)).isoformat()
            save_state(state)
            DATA.mkdir(parents=True, exist_ok=True)
            with (DATA / "run_history.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": kind, "slot": slot, "started_at": started,
                                         "finished_at": current.isoformat(), "success": success,
                                         "results": results, "test_mode": is_test()}) + "\n")
            del self.running[kind]
        slots = {"data": current.strftime("%Y-%m-%d"), "comment": current.strftime("%Y-%m-%dT%H")}
        for kind, function in (("comment", run_comment_cycle), ("data", run_data_collection)):
            if kind in self.running or state.get(f"last_{kind}_slot") == slots[kind]:
                continue
            if kind == "data" and current.hour < int(os.environ.get("DATA_COLLECTION_HOUR", "8")):
                continue
            retry_after = state.get(f"{kind}_retry_after")
            if retry_after:
                try:
                    if datetime.fromisoformat(retry_after) > current:
                        continue
                except ValueError:
                    pass
            self.running[kind] = (self.executor.submit(function), slots[kind], current.isoformat())


def main() -> None:
    parser = argparse.ArgumentParser(description="采集任务容器调度器")
    parser.add_argument("--run", choices=("data", "comments", "all"), help="立即执行一次后退出")
    args = parser.parse_args()
    with scheduler_guard():
        if args.run in {"data", "all"}:
            results = run_data_collection()
            if any(results.values()):
                raise SystemExit(1)
        if args.run in {"comments", "all"}:
            results = run_comment_cycle()
            if any(results.values()):
                raise SystemExit(1)
        if not args.run:
            scheduler_loop()



if __name__ == "__main__":
    main()
