"""Retain known historical fields without presenting them as freshly fetched."""
import copy
import hashlib
import json
from runtime import DATA
from .credentials import private_json
from .base import now, ProviderError


def archive_authorized_replacement(account, cached_account, cached_records, collected, settings):
    """Stage a verified replacement; only a committed snapshot activates retirement.

    The archive alone never authorizes comment deletion. Its digest must also be
    present on the successfully published replacement account in the snapshot.
    """
    if account.get("platform") != "douyin" or not cached_account:
        return None
    previous = str(cached_account.get("platform_uid") or "")
    current = str(account.get("platform_uid") or "")
    old_official = str(cached_account.get("official_user_id") or "")
    profile = collected.profile
    new_official = str(profile.get("official_user_id") or "")
    if previous == current:
        if old_official and old_official != new_official:
            raise ProviderError("identity_mismatch", "抖音内部账号标识发生变化，未经授权不得覆盖旧快照")
        return None
    if not previous or str(settings.get("replaces_platform_uid") or "") != previous:
        raise ProviderError("identity_mismatch", "抖音账号标识发生变化，但未明确授权替换该旧账号")
    if (not current or str(profile.get("verified_account_id") or "") != current
            or not new_official or str(settings.get("expected_uid") or "") != new_official):
        raise ProviderError("identity_mismatch", "新抖音号及内部账号标识必须与绑定一致后才能替换")
    if not collected.complete:
        raise ProviderError("incomplete_pagination", "账号替换必须完成全量采集；本轮保留旧快照")
    new_ids = sorted({str(row.get("video_id") or "") for row in collected.records})
    if "" in new_ids or len(new_ids) != len(collected.records):
        raise ProviderError("schema_changed", "账号替换的新目录包含空或重复作品标识")
    payload = {"reason": "explicit_account_replacement", "account_key": f"douyin:{account['account_name']}",
               "previous_account": cached_account, "previous_records": cached_records,
               "verified_account_id": current, "verified_official_user_id": new_official,
               "replacement_content_ids": new_ids}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    path = DATA / "account_replacements" / (digest + ".json")
    if not path.exists():
        private_json(path, {**payload, "replacement_id": digest, "archived_at": now()})
    return digest


def retire_replaced_comment_contents(contents, state, timeline, snapshot):
    """Run in the comment lane before merging discoveries; caller saves its state.

    Archive each removed state fragment before mutating any input. Unpublished
    candidate archives have no effect. Repeated calls also filter reintroduced
    discoveries; overlapping current content IDs retain their comment state.
    """
    retired = set()
    protected = {(row.get("platform"), str(row.get("video_id")))
                 for row in snapshot.get("videos", [])}
    current_contents = {(row.get("account_key"), str(row.get("video_id")))
                        for row in snapshot.get("videos", [])}
    for account in snapshot.get("accounts", []):
        digest = account.get("account_replacement_id", "")
        if (account.get("platform") != "douyin" or not isinstance(digest, str) or len(digest) != 24
                or any(c not in "0123456789abcdef" for c in digest)):
            continue
        path = DATA / "account_replacements" / (digest + ".json")
        archive = json.loads(path.read_text(encoding="utf-8"))
        if (archive.get("replacement_id") != digest or archive.get("reason") != "explicit_account_replacement"
                or archive.get("account_key") != account.get("account_key")
                or archive.get("verified_account_id") != str(account.get("platform_uid") or "")
                or archive.get("verified_official_user_id") != str(account.get("official_user_id") or "")):
            raise ProviderError("identity_mismatch", "退休目录与当前账号快照不匹配，暂停评论扫描")
        new_ids = set(archive["replacement_content_ids"])
        retired.update((account["account_key"], "douyin", str(row.get("video_id")))
                       for row in archive["previous_records"]
                       if str(row.get("video_id") or "") and str(row["video_id"]) not in new_ids
                       and (account["account_key"], str(row["video_id"])) not in current_contents)
    def is_retired(item):
        return (item.get("account_key"), item.get("platform"), str(item.get("content_id"))) in retired
    # State keys predate account scoping, so preserve keys still used by any live account.
    keys = {f"{platform}:{cid}" for _, platform, cid in retired if (platform, cid) not in protected}
    removed = {name: {k: v for k, v in state.get(name, {}).items() if k in keys}
               for name in ("seen_comments", "content_counts", "content_poll_at")}
    removed["root_reply_counts"] = {k: v for k, v in state.get("root_reply_counts", {}).items()
                                    if any(k.startswith(prefix + ":") for prefix in keys)}
    removed["full_scan_baselines"] = [k for k in state.get("full_scan_baselines", []) if k in keys]
    removed["discovered_contents"] = [item for item in state.get("discovered_contents", []) if is_retired(item)]
    events = {k: v for k, v in timeline.get("events", {}).items() if is_retired(v)}
    if any(removed.values()) or events:
        payload = {"retired_contents": sorted(retired), "state": removed, "timeline_events": events}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
        path = DATA / "account_replacements" / "comments" / (digest + ".json")
        if not path.exists():
            private_json(path, {**payload, "archived_at": now()})
        for name in ("seen_comments", "content_counts", "content_poll_at", "root_reply_counts"):
            state[name] = {k: v for k, v in state.get(name, {}).items() if k not in removed[name]}
        state["full_scan_baselines"] = [k for k in state.get("full_scan_baselines", []) if k not in keys]
        state["discovered_contents"] = [item for item in state.get("discovered_contents", []) if not is_retired(item)]
        timeline["events"] = {k: v for k, v in timeline.get("events", {}).items() if k not in events}
    return [item for item in contents if not is_retired(item)]


def channel_namespace_changed(cached_records, records):
    return (bool(cached_records) and bool(records)
            and any(str(row.get("video_id", "")).isdigit() for row in cached_records)
            and all(str(row.get("video_id", "")).startswith("export/") for row in records))


def archive_channel_namespace(cached_account, cached_records):
    digest = hashlib.sha256(json.dumps(cached_records, sort_keys=True).encode()).hexdigest()[:20]
    path = DATA / "identity_migrations" / (digest + ".json")
    if not path.exists():
        private_json(path, {"archived_at": now(), "reason": "native_export_id_namespace",
                            "previous_account": cached_account, "previous_records": cached_records,
                            "automatic_cross_content_mapping": False})
    return path


def quarantine_mismatched_channel(cached_account, cached_records, verified_profile):
    """Isolate conflicting channel identifiers; do not infer ownership from names.

    A conflict is evidence for review, not proof that two API identity schemes
    refer to different people. Preserve the original records for reconciliation.
    """
    if not cached_account or cached_account.get("platform") != "wechat_channels":
        return None
    previous = str(cached_account.get("official_user_id") or cached_account.get("platform_uid") or "")
    current = str(verified_profile.get("official_user_id") or "")
    if not (previous.startswith("v2_") and current.startswith("v2_") and previous != current):
        return None
    key = str(cached_account.get("account_key"))
    digest = hashlib.sha256((key + previous).encode()).hexdigest()[:20]
    path = DATA / "identity_quarantine" / (digest + ".json")
    if not path.exists():
        private_json(path, {"quarantined_at": now(), "reason": "platform_identity_conflict_requires_review",
                            "previous_account": cached_account, "previous_records": cached_records,
                            "verified_account_id": verified_profile.get("verified_account_id"),
                            "verified_official_user_id": current})
    return path


def retain_known(records, previous, id_field):
    cached = {str(r.get(id_field)): r for r in previous}
    result = []
    for original in records:
        row = copy.deepcopy(original)
        old = cached.get(str(row.get(id_field)), {})
        if isinstance(old.get("historical_metrics"), dict):
            row.setdefault("historical_metrics", copy.deepcopy(old["historical_metrics"]))
        restored = []
        for field in ("published_at", "cover", "url", "title", "source_author", "source_author_id", "aid", "cid"):
            if row.get(field) in (None, "") and old.get(field) not in (None, ""):
                row[field] = old[field]
                restored.append(field)
        for metric, value in row.get("stats", {}).items():
            historical = old.get("stats", {}).get(metric)
            if value is None and historical is not None:
                if row.get("metric_provenance", {}).get(metric, {}).get("missing_reason") == "incompatible_unit":
                    row.setdefault("historical_metrics", {})[metric] = {
                        "value": historical, "source": old.get("data_source"),
                        "fetched_at": old.get("fetched_at"), "reason": "incompatible_unit"}
                    continue
                row["stats"][metric] = historical
                row.setdefault("metric_provenance", {})[metric] = {
                    "source": "cached", "fetched_at": old.get("fetched_at"),
                    "missing_reason": "not_returned_in_current_collection"}
                restored.append("stats." + metric)
        if restored:
            row["cached_fields"] = restored
        result.append(row)
    return result
