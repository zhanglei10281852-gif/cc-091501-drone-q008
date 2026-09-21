"""时间工具：统一使用带时区的时间（UTC 内部表示）。"""

from datetime import datetime, timezone


def parse(value: str | datetime) -> datetime:
    """解析 ISO 8601 字符串；拒绝无时区时间（领域约定）。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    else:
        raise ValueError(f"不支持的时间类型: {type(value)!r}")
    if dt.tzinfo is None:
        raise ValueError(f"时间必须带时区: {value!r}")
    return dt.astimezone(timezone.utc)


def format_value(dt: datetime) -> str:
    """输出带时区的 ISO 8601 UTC 字符串（Z 结尾）。"""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def now() -> datetime:
    return datetime.now(timezone.utc)
