#!/usr/bin/env python3
"""Account readiness and observation-period gate; never contacts platforms."""
import argparse
import json
from datetime import datetime
from runtime import DATA
from provider_setup import accounts
from providers.settings import SettingsStore
from providers.health import read_verification
from providers.public_articles import PUBLIC_ARTICLE_PLATFORMS, public_article_settings


def report(min_days=7):
    settings = SettingsStore().read()["accounts"]
    catalog = accounts(include_public=True)
    rows = [{"account": key, "scope": "public_article" if account["platform"] in PUBLIC_ARTICLE_PLATFORMS else "provider",
             **read_verification(key, public_article_settings(account)
                                 if account["platform"] in PUBLIC_ARTICLE_PLATFORMS else settings.get(key, {}))}
            for key, account in catalog.items()]
    history = []
    path = DATA / "run_history.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
                datetime.fromisoformat(row["finished_at"])
                history.append(row)
            except (ValueError, KeyError, TypeError):
                continue
    observations = {}
    for kind in ("data", "comment"):
        jobs = [x for x in history if x.get("kind") == kind]
        times = sorted(datetime.fromisoformat(x["finished_at"]) for x in jobs)
        days = (times[-1] - times[0]).total_seconds() / 86400 if len(times) > 1 else 0
        rate = sum(x.get("success") is True for x in jobs) / len(jobs) if jobs else 0
        # Calendar gaps cannot be hidden by two isolated successful runs.
        observed_days = len({x.date() for x in times})
        passed = days >= min_days and observed_days >= min_days and rate >= 0.95
        observations[kind] = {"runs": len(jobs), "span_days": round(days, 2),
                              "days_with_runs": observed_days, "success_rate": round(rate, 4), "passed": passed}
    account_ready = all(x["ready"] for x in rows)
    return {"accounts": rows, "ready_accounts": sum(x["ready"] for x in rows), "total_accounts": len(rows),
            "scope": "all_configured_video_and_article_accounts",
            "accounts_ready": account_ready, "observations": observations,
            "release_ready": account_ready and all(x["passed"] for x in observations.values())}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--require-ready", action="store_true")
    p.add_argument("--min-observation-days", type=int, default=7)
    args = p.parse_args()
    if args.min_observation_days < 1:
        p.error("观察期至少 1 天")
    result = report(args.min_observation_days)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.require_ready and not result["release_ready"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
