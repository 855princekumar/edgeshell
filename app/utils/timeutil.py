"""Time helpers.

Storage rule: every timestamp in the database is naive UTC (datetime.utcnow()).
Display rule: server-rendered pages convert to DISPLAY_TZ (default Asia/Kolkata);
JSON APIs emit ISO-8601 with an explicit 'Z' so browsers convert to local time
instead of guessing (a naive ISO string is parsed as *local* time by JS, which
made UTC values look like local ones).
"""
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

_tz_cache = {}


def get_tz(name):
    if not name or ZoneInfo is None:
        return timezone.utc
    if name not in _tz_cache:
        try:
            _tz_cache[name] = ZoneInfo(name)
        except Exception:
            _tz_cache[name] = timezone.utc
    return _tz_cache[name]


def iso_utc(dt):
    """Naive-UTC or aware datetime -> 'YYYY-MM-DDTHH:MM:SSZ' (None-safe)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0).isoformat() + 'Z'


def to_local(dt, tz_name):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_tz(tz_name))


def tz_label(tz_name):
    """Short label such as 'IST' for column headers."""
    try:
        return datetime.now(get_tz(tz_name)).strftime('%Z') or tz_name
    except Exception:
        return tz_name
