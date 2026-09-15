"""Retain known historical fields without presenting them as freshly fetched."""
import copy
import hashlib
import json
from runtime import DATA
from .credentials import private_json
from .base import now


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
        restored = []
        for field in ("published_at", "cover", "url", "title", "source_author", "source_author_id", "aid", "cid"):
            if row.get(field) in (None, "") and old.get(field) not in (None, ""):
                row[field] = old[field]
                restored.append(field)
        for metric, value in row.get("stats", {}).items():
            historical = old.get("stats", {}).get(metric)
            if value is None and historical is not None:
                row["stats"][metric] = historical
                row.setdefault("metric_provenance", {})[metric] = {
                    "source": "cached", "fetched_at": old.get("fetched_at"),
                    "missing_reason": "not_returned_in_current_collection"}
                restored.append("stats." + metric)
        if restored:
            row["cached_fields"] = restored
        result.append(row)
    return result
