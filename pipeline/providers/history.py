"""Retain known historical fields without presenting them as freshly fetched."""
import copy


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
