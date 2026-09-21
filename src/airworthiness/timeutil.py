"""时间处理：统一使用带时区的 ISO 8601 字符串。"""

from datetime import datetime, timezone

from .errors import ValidationError

#: 开放式区间的远端哨兵（如未关闭的维修占用）。
FAR_FUTURE = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


def parse_ts(value, field="time"):
    """解析带时区的 ISO 8601 时间，拒绝朴素（无时区）时间。"""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ValidationError(f"字段 {field} 不是有效的 ISO 8601 时间: {value!r}") from None
    else:
        raise ValidationError(f"字段 {field} 缺少有效时间")
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValidationError(f"字段 {field} 必须携带时区")
    return parsed


def iso(dt):
    return dt.isoformat() if dt is not None else None


def now_utc():
    return datetime.now(timezone.utc)
