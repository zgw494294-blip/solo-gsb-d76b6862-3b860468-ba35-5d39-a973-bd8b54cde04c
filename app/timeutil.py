"""Monotonic-ish UTC timestamps stored as integer microseconds since epoch.

Microsecond integers are cheap to compare/sort in SQLite; API responses
additionally carry ISO-8601 strings for readability.
"""
from __future__ import annotations

from datetime import datetime, timezone


def now_us() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1_000_000)


def iso(ts_us: int) -> str:
    return datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
