#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
平台直采视频数据采集器
=====================
从授权平台后台及公开详情获取抖音 / B站 / 微信视频号账号的全部作品数据,
归一化为统一 schema 后写入 data/dashboard_data.json 供前端大屏使用。

用法:
    python3 pipeline/provider_setup.py status  # 检查各账号接入状态
    python3 pipeline/fetch_data.py                    # 全量采集
    python3 pipeline/fetch_data.py --mock             # 生成演示数据(无需API key)
    python3 pipeline/fetch_data.py --no-enrich-bili   # B站不逐条补点赞/投币(减少请求)
    python3 pipeline/fetch_data.py --debug            # 保存原始响应到 data/debug/

注意: 各账号需要配置已核验的 provider 和独立登录会话。
"""

import argparse
import json
import re
import os
import random
import sys
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

from snapshot_utils import (
    atomic_write_json,
    atomic_write_text,
    finalize_snapshot,
    is_suspicious_drop,
    merge_records,
)

try:
    import requests
except ImportError:
    sys.exit("缺少依赖: pip3 install requests")

from runtime import DATA, WEB, load_env as load_dotenv
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "accounts.json"
OUT_PATH = DATA / "dashboard_data.json"
DEBUG_DIR = DATA / "debug"
from providers import ProviderRegistry
from providers.history import retain_known
from providers.health import record_verification
CN_TZ = timezone(timedelta(hours=8))


def norm_name(s: str) -> str:
    """宽松比较账号名: 去空白、统一大小写与全半角。"""
    return unicodedata.normalize("NFKC", s or "").replace(" ", "").lower()


def to_int(v, default=None):
    try:
        return int(str(v).replace(",", "").replace("+", ""))
    except (TypeError, ValueError):
        return default


def epoch_to_iso(ts):
    ts = to_int(ts)
    if not ts:
        return None
    if ts > 10_000_000_000:
        ts //= 1000
    return datetime.fromtimestamp(ts, tz=CN_TZ).isoformat()


def duration_seconds(value):
    """兼容秒数、毫秒数以及 01:23 / 1:02:03 时长文本。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number // 1000 if number > 100_000 else number
    text = str(value).strip()
    if ":" in text:
        try:
            parts = [int(part) for part in text.split(":")]
        except ValueError:
            return None
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return to_int(text)


def dig(obj, *paths, default=None):
    """按多个候选路径取值, 返回第一个非 None 的。"""
    for path in paths:
        cur = obj
        ok = True
        for key in path.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            elif isinstance(cur, list) and key.isdigit() and int(key) < len(cur):
                cur = cur[int(key)]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default


def unwrap_data(obj):
    """解开 历史包装以及平台常见的 code/message/data 包装。"""
    cur = obj
    for _ in range(5):
        if not isinstance(cur, dict) or not isinstance(cur.get("data"), dict):
            break
        # 历史响应外层可能带 request_id/router；B站上游常见 code/message/ttl/data。
        if "request_id" in cur or "router" in cur or set(cur).issubset({"code", "message", "ttl", "data"}):
            cur = cur["data"]
        else:
            break
    return cur


def parse_json_object(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def compact_error(exc, limit=500):
    text = re.sub(r"\s+", " ", str(exc)).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def first_nonempty(*values, default=""):
    """返回第一个非 None 且非空字符串/容器的值。"""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        return value
    return default


def wechat_title(value, video_id=""):
    """视频号 title 可能是字符串，也可能是 [{shortTitle: ...}]。"""
    if isinstance(value, str):
        title = value.strip()
    elif isinstance(value, list):
        title = ""
        for item in value:
            if isinstance(item, dict):
                title = str(item.get("shortTitle") or item.get("title") or "").strip()
                if title:
                    break
    elif isinstance(value, dict):
        title = str(value.get("shortTitle") or value.get("title") or "").strip()
    else:
        title = ""
    return title or (f"视频号作品 {video_id}" if video_id else "视频号作品")


PLATFORM_LABEL = {"douyin": "抖音", "bilibili": "B站", "wechat_channels": "视频号"}


def collect(args, registry=None) -> dict:
    client = registry or ProviderRegistry()
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    run_at = datetime.now(CN_TZ).isoformat()
    previous = {"accounts": [], "videos": []}
    previous_path = Path(args.out)
    if previous_path.exists():
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    result = {
        "updated_at": run_at,
        "source": "platform_direct",
        "refresh_scope": list(getattr(args, "only", None) or []) or "all",
        "accounts": [],
        "videos": [],
        "api_calls": 0,
        "warnings": [],
    }
    for acc in cfg["accounts"]:
        key = f"{acc['platform']}:{acc['account_name']}"
        cached_videos = [v for v in previous.get("videos", []) if v.get("account_key") == key]
        cached_account = next((a for a in previous.get("accounts", [])
                               if a.get("account_key") == key), None)
        only = set(getattr(args, "only", None) or [])
        if only and not ({key, acc["platform"], acc["account_name"]} & only):
            entry = dict(cached_account or {**acc, "account_key": key, "status": "pending"})
            entry["refreshed_in_run"] = False
            result["accounts"].append(entry)
            result["videos"].extend([{**row, "snapshot_state": "cached"} for row in cached_videos])
            continue
        account_started = time.monotonic()
        calls_before = client.call_count
        print(f">>> 采集 {PLATFORM_LABEL[acc['platform']]} / {acc['business_line']} / {acc['account_name']}")
        entry = {**acc, "account_key": key,
                 "platform_label": PLATFORM_LABEL[acc["platform"]],
                 "followers": None, "total_videos": 0, "status": "ok", "error": "",
                 "last_attempt_at": run_at, "refreshed_in_run": False}
        provider_settings = dict(client.config.get("accounts", {}).get(key, {}))
        try:
            provider = client.get(acc)
            if acc["platform"] == "bilibili" and hasattr(provider, "http"):
                provider.settings["enrich"] = not args.no_enrich_bili
                provider.http.interval = max(0.1, getattr(args, "interval", 0.6))
            collected = provider.collect(max_pages=getattr(args, "max_pages", 200))
            record_verification(key, provider_settings, collected)
            info, vids = collected.profile, collected.records
            entry["data_source"] = collected.source
            if collected.complete and is_suspicious_drop(len(vids), len(cached_videos)):
                raise RuntimeError(
                    f"本次仅返回 {len(vids)} 条，较上次 {len(cached_videos)} 条异常下降；"
                    "为避免不完整响应覆盖历史快照，已中止替换")
            vids = retain_known(vids, cached_videos, "video_id")
            for video in vids:
                video["account_key"] = key
                video["snapshot_state"] = "current"
            if not collected.complete:
                vids, restored = merge_records(vids, cached_videos, "video_id")
                entry.update({"status": "partial", "error": collected.note,
                              "coverage_note": f"部分覆盖，保留历史 {restored} 条"})
                result["warnings"].append({"account_key": key, "message": collected.note})
            entry["followers"] = info.get("followers")
            entry["nickname"] = info.get("nickname")
            entry["total_videos"] = len(vids)
            entry["last_success_at"] = run_at
            entry["refreshed_in_run"] = True
            for v in vids:
                v.update({"account_key": key, "platform": acc["platform"],
                          "platform_label": PLATFORM_LABEL[acc["platform"]],
                          "business_line": acc["business_line"],
                          "account_name": acc["account_name"],
                          "snapshot_state": v.get("snapshot_state", "current")})
            result["videos"].extend(vids)
            print(f"    作品数: {len(vids)}")
        except Exception as e:
            record_verification(key, provider_settings, error=e)
            message = compact_error(e)
            if cached_account and cached_videos:
                # 单账号瞬时失败时保留上一次完整快照，避免本轮采集把线上大屏数据清空。
                entry.update({
                    "followers": cached_account.get("followers"),
                    "nickname": cached_account.get("nickname", acc["account_name"]),
                    "platform_uid": cached_account.get("platform_uid", acc.get("platform_uid", "")),
                    "total_videos": len(cached_videos),
                    "status": "stale",
                    "error": f"本次刷新失败，已保留上次数据：{message}",
                    "last_success_at": cached_account.get("last_success_at")
                                       or previous.get("updated_at"),
                })
                result["videos"].extend(
                    [{**video, "snapshot_state": "cached"} for video in cached_videos])
                print(f"    [警告] {entry['error']}", file=sys.stderr)
            else:
                entry["status"] = "error"
                entry["error"] = message
                print(f"    [错误] {entry['error']}", file=sys.stderr)
            result["warnings"].append({"account_key": key, "message": entry["error"]})
        entry["request_count"] = client.call_count - calls_before
        entry["collection_seconds"] = round(time.monotonic() - account_started, 2)
        result["accounts"].append(entry)
    result["provider_requests"] = client.call_count
    result["api_calls"] = 0
    result["ok_accounts"] = sum(1 for a in result["accounts"] if a["status"] == "ok")
    result["stale_accounts"] = sum(1 for a in result["accounts"] if a["status"] == "stale")
    result["partial_accounts"] = sum(1 for a in result["accounts"] if a["status"] == "partial")
    result["error_accounts"] = sum(1 for a in result["accounts"] if a["status"] == "error")
    success_times = [a.get("last_success_at") for a in result["accounts"] if a.get("last_success_at")]
    result["data_as_of"] = min(success_times) if success_times else None
    result["latest_success_at"] = max(success_times) if success_times else None
    finalize_snapshot(result, "video")
    print(f"完成, 共调用 API {client.call_count} 次, 作品 {len(result['videos'])} 条")
    return result


def make_mock() -> dict:
    random.seed(42)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    topics = {
        "望获": ["望获OS 实时内核深度解析", "望获OS VxWorks 迁移实战", "工业现场总线适配教程",
                 "望获OS 在数控机床上的落地", "硬实时任务调度原理", "嵌入式虚拟化技术分享",
                 "望获OS 开发者大会回顾", "微内核与宏内核对比"],
        "芯片": ["RISC-V 安全启动全链路", "车规级 MCU 功能安全入门", "国密算法硬件加速实践",
                 "芯片 trusted boot 演示", "侧信道攻击与防护", "HSM 安全模块设计",
                 "汽车电子 ISO26262 解读", "安全芯片选型指南"],
    }
    now = datetime.now(CN_TZ)
    videos, accounts = [], []
    for acc in cfg["accounts"]:
        key = f"{acc['platform']}:{acc['account_name']}"
        n = random.randint(18, 42)
        if acc["platform"] == "wechat_channels":
            base = random.randint(800, 12000)
        else:
            base = random.randint(3000, 50000)
        entry = {**acc, "account_key": key,
                 "platform_label": PLATFORM_LABEL[acc["platform"]],
                 "followers": random.randint(1500, 68000), "total_videos": n,
                 "status": "ok", "error": "", "nickname": acc["account_name"]}
        accounts.append(entry)
        for i in range(n):
            day_offset = int(random.betavariate(1.6, 2.2) * 540)
            pub = now - timedelta(days=day_offset, hours=random.randint(0, 20))
            heat = random.betavariate(0.7, 5)
            if acc["platform"] == "wechat_channels":
                play = None
                like_base = base * heat * 0.35
            else:
                play = int(base * heat * random.uniform(0.4, 2.6))
                like_base = play
            like = int(like_base * random.uniform(0.015, 0.09))
            comment = int(like * random.uniform(0.08, 0.45))
            share = int(like * random.uniform(0.1, 0.6))
            collect = int(like * random.uniform(0.2, 0.9))
            stats = {"play": play, "like": like, "comment": comment,
                     "share": share, "collect": collect}
            if acc["platform"] == "douyin":
                stats["download"] = int(like * random.uniform(0.02, 0.2))
            if acc["platform"] == "bilibili":
                stats["danmaku"] = int(comment * random.uniform(0.5, 2.5))
                stats["coin"] = int(like * random.uniform(0.1, 0.5))
            title = f"{random.choice(topics[acc['business_line']])} 第{random.randint(1, 30)}期"
            videos.append({
                "account_key": key, "platform": acc["platform"],
                "platform_label": PLATFORM_LABEL[acc["platform"]],
                "business_line": acc["business_line"],
                "account_name": acc["account_name"],
                "video_id": f"mock_{acc['platform']}_{i}",
                "title": title, "cover": "", "url": "",
                "published_at": pub.isoformat(),
                "duration": random.randint(35, 720),
                "stats": stats,
            })
    return {"updated_at": now.isoformat(), "source": "mock",
            "accounts": accounts, "videos": videos}


def main():
    ap = argparse.ArgumentParser(description="平台直采视频数据采集器")
    ap.add_argument("--mock", action="store_true", help="生成演示数据(不调用 API)")
    ap.add_argument("--debug", action="store_true", help="保存原始 API 响应到 data/debug/")
    ap.add_argument("--no-enrich-bili", action="store_true",
                    help="B站不逐条补全点赞/投币(减少请求)")
    ap.add_argument("--interval", type=float, default=0.6, help="API 调用最小间隔秒数")
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--only", action="append", help="仅刷新指定平台、账号名或 account_key")
    ap.add_argument("--no-publish-web", action="store_true", help="不写入网页快照")
    ap.add_argument("--out", default=str(OUT_PATH), help="输出 JSON 路径")
    args = ap.parse_args()

    if args.mock:
        data = finalize_snapshot(make_mock(), "video")
    else:
        load_dotenv()
        data = collect(args)

    out = Path(args.out)
    atomic_write_json(out, data, pretty=True)
    if not args.no_publish_web:
        web_data = WEB / "data" / "dashboard_data.json"
        atomic_write_json(web_data, data)
        # file:// 直开模式: 浏览器无法 fetch 本地 JSON, 注入为全局变量
        web_js = WEB / "data" / "dashboard_data.js"
        atomic_write_text(
            web_js,
            "window.__DASHBOARD_DATA__ = " + json.dumps(data, ensure_ascii=False) + ";\n",
        )
        print(f"已写入 {out} 与 {web_data}(.js)")
    if not args.mock and any(a.get("last_attempt_at") == data.get("updated_at") and a.get("status") != "ok"
                             for a in data.get("accounts", [])):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
