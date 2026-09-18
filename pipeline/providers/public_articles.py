"""Readiness evidence for the article collectors that predate provider migration."""
from .base import Collection, now
from .health import record_verification

PUBLIC_ARTICLE_PLATFORMS = {"csdn", "elecfans", "sohu", "toutiao"}


def public_article_settings(account):
    return {"provider": account["platform"] + "_public",
            "expected_uid": str(account.get("platform_uid") or ""),
            "profile_url": account.get("profile_url", ""),
            "required_metrics": ["read", "comment"]}


def record_public_article_verification(account, entry=None, records=None, error=None, directory=None):
    """Assess raw observed values before any historical metric/cache restoration."""
    key = f"{account['platform']}:{account['account_name']}"
    settings = public_article_settings(account)
    result = None
    if entry is not None and error is None:
        profile = dict(entry)
        actual = str(profile.get("verified_account_id") or "")
        if not actual or actual != settings["expected_uid"]:
            profile.pop("verified_account_id", None)
        result = Collection(profile, records or [], entry.get("status") == "ok",
                            note=entry.get("coverage_note", ""), source=settings["provider"])
    return record_verification(key, settings, result=result, error=error, directory=directory)


def annotate_public_articles(account, records):
    source = public_article_settings(account)["provider"]
    checked_at = now()
    return [{**row, "data_source": source, "fetched_at": checked_at,
             "metric_provenance": {**{key: {"source": source} for key in row.get("stats", {})},
                                   **row.get("metric_provenance", {})}}
            for row in records]
