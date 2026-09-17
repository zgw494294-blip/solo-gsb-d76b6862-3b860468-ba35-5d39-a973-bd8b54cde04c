"""Runtime configuration loaded from environment variables.

All quota amounts are integer "units" (integers avoid floating point
rounding errors in accounting math).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _bool_env(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_path: str
    default_pool_total: int
    init_default_pool: bool
    busy_timeout_ms: int
    default_ttl_seconds: int
    max_ttl_seconds: int


def load_settings() -> Settings:
    return Settings(
        db_path=os.environ.get("QUOTA_DB_PATH", "/data/quota.db"),
        default_pool_total=_int_env("QUOTA_POOL_TOTAL", 1_000_000),
        init_default_pool=_bool_env("QUOTA_INIT_DEFAULT_POOL", True),
        busy_timeout_ms=_int_env("QUOTA_BUSY_TIMEOUT_MS", 5_000),
        default_ttl_seconds=_int_env("QUOTA_DEFAULT_TTL_SECONDS", 300),
        max_ttl_seconds=_int_env("QUOTA_MAX_TTL_SECONDS", 86_400),
    )
