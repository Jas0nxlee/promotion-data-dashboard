"""Explicit native-to-canonical ID mapping, never fuzzy name matching."""
from urllib.parse import urlsplit, parse_qs, urlencode, urlunsplit
from .base import ProviderError, identifier


def validate_aliases(aliases):
    if not isinstance(aliases, dict):
        raise ProviderError("invalid_mapping", "ID 映射必须是对象")
    result = {identifier(k): identifier(v) for k, v in aliases.items()}
    if any(not k or not v for k, v in result.items()) or len(set(result.values())) != len(result):
        raise ProviderError("invalid_mapping", "ID 映射必须非空且一对一")
    for native, canonical in result.items():
        if native != canonical and canonical in result and result[canonical] != canonical:
            raise ProviderError("invalid_mapping", "不允许链式或循环 ID 映射")
    return result


def native_id(value, aliases):
    mapping = validate_aliases(aliases)
    reverse = {v: k for k, v in mapping.items()}
    return reverse.get(str(value), str(value))


def canonical_url(url):
    parsed = urlsplit(url or "")
    if not parsed.hostname:
        return ""
    query = parse_qs(parsed.query)
    # Only platform content identity parameters; discard tracking and expiring tokens.
    params = {k: query[k] for k in ("__biz", "mid", "idx", "aid", "bvid") if k in query}
    return urlunsplit(("https", parsed.hostname.lower(), parsed.path.rstrip("/"), urlencode(params, doseq=True), ""))


def reconcile_contents(previous, incoming, id_field):
    old_by_url = {}
    for row in previous:
        url = canonical_url(row.get("url"))
        if url:
            old_by_url.setdefault(url, set()).add(str(row[id_field]))
    aliases, unresolved, unchanged = {}, [], 0
    old_ids = {str(r[id_field]) for r in previous}
    for row in incoming:
        new_id = identifier(row.get(id_field))
        if new_id in old_ids:
            unchanged += 1
            continue
        candidates = old_by_url.get(canonical_url(row.get("url")), set())
        if len(candidates) == 1:
            aliases[new_id] = next(iter(candidates))
        else:
            unresolved.append(new_id)
    validate_aliases(aliases)
    return {"content_aliases": aliases, "unchanged": unchanged, "unresolved_or_new": unresolved,
            "rule": "exact_platform_url", "automatic_state_changes": False}
