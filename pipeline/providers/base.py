from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

CN_TZ = timezone(timedelta(hours=8))


class ProviderError(RuntimeError):
    def __init__(self, reason, message):
        self.reason = reason
        super().__init__(f"{reason}: {message}")


def pick(value, *paths, default=None):
    for path in paths:
        node = value
        for part in path.split(".") if path else []:
            if isinstance(node, dict):
                node = node.get(part)
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                node = None
                break
        if node is not None:
            return node
    return default


def identifier(value):
    if value is None or value == "":
        return ""
    if isinstance(value, (float, bool)):
        raise ProviderError("schema_changed", "标识必须为字符串或整数，不能经过浮点转换")
    return str(value)


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value).replace(",", ""))
        return int(n) if n.is_finite() and n >= 0 and n == n.to_integral_value() else None
    except (TypeError, ValueError, InvalidOperation):
        return None


def timestamp(value):
    if value in (None, "", 0, "0"):
        return None
    try:
        n = float(value)
        if n > 10**12:
            n /= 1000
        return datetime.fromtimestamp(n, CN_TZ).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        try:
            d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return d.replace(tzinfo=d.tzinfo or CN_TZ).astimezone(CN_TZ).isoformat()
        except ValueError:
            return None


def now():
    return datetime.now(CN_TZ).isoformat()


@dataclass
class Collection:
    profile: dict
    records: list
    complete: bool = True
    note: str = ""
    source: str = "browser"
    request_count: int = 0


@dataclass
class Pages:
    rows: list
    count: int
    complete: bool
    note: str = ""
    envelopes: list = field(default_factory=list)


def unique(rows, key):
    result, seen = [], set()
    for row in rows:
        value = identifier(row.get(key))
        if not value:
            raise ProviderError("schema_changed", f"响应缺少 {key}，不能静默丢弃记录")
        if value not in seen:
            seen.add(value)
            result.append(row)
    return result
