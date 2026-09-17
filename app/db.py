"""SQLite connection management, schema and the write-transaction helper.

Concurrency model
-----------------
Every state-changing operation runs in a single ``BEGIN IMMEDIATE``
transaction, which acquires the database RESERVED lock up-front.  With
WAL journal mode readers never block, writers fully serialize, and the
``busy_timeout`` makes contending writers wait instead of failing.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .config import Settings, load_settings
from .timeutil import now_us

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS quota_pools (
    id             TEXT PRIMARY KEY,
    total          INTEGER NOT NULL CHECK (total >= 0),
    available      INTEGER NOT NULL CHECK (available >= 0),
    held           INTEGER NOT NULL DEFAULT 0 CHECK (held >= 0),
    used           INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
    created_at_us  INTEGER NOT NULL,
    CHECK (available + held + used = total)
);

CREATE TABLE IF NOT EXISTS reservations (
    id                    TEXT PRIMARY KEY,
    pool_id               TEXT NOT NULL REFERENCES quota_pools(id),
    amount                INTEGER NOT NULL CHECK (amount > 0),
    used_amount           INTEGER NOT NULL DEFAULT 0 CHECK (used_amount >= 0),
    status                TEXT NOT NULL CHECK (status IN ('held','settled','released','expired')),
    expires_at_us         INTEGER NOT NULL,
    created_at_us         INTEGER NOT NULL,
    finalized_at_us       INTEGER,
    idempotency_key       TEXT NOT NULL,
    CHECK (
        (status = 'held'     AND used_amount = 0          AND finalized_at_us IS NULL)
     OR (status = 'settled'  AND used_amount <= amount     AND finalized_at_us IS NOT NULL)
     OR (status = 'released' AND used_amount = 0           AND finalized_at_us IS NOT NULL)
     OR (status = 'expired'  AND used_amount = 0           AND finalized_at_us IS NOT NULL)
    )
);

-- Idempotency keys are unique within a (pool, operation scope).
CREATE UNIQUE INDEX IF NOT EXISTS ux_reservations_pool_key
    ON reservations(pool_id, idempotency_key);
CREATE INDEX IF NOT EXISTS ix_reservations_pool_status
    ON reservations(pool_id, status);

-- Append-only ledger. UPDATE/DELETE triggers below reject any mutation.
CREATE TABLE IF NOT EXISTS ledger_entries (
    seq                INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_id            TEXT NOT NULL REFERENCES quota_pools(id),
    reservation_id     TEXT,
    type               TEXT NOT NULL,
    amount             INTEGER NOT NULL,
    delta_available    INTEGER NOT NULL,
    delta_held         INTEGER NOT NULL,
    delta_used         INTEGER NOT NULL,
    created_at_us      INTEGER NOT NULL,
    CHECK (delta_available + delta_held + delta_used = 0)
);

CREATE TRIGGER IF NOT EXISTS trg_ledger_no_update
BEFORE UPDATE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger_entries is immutable: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_ledger_no_delete
BEFORE DELETE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger_entries is immutable: DELETE is forbidden');
END;

CREATE INDEX IF NOT EXISTS ix_ledger_pool_seq
    ON ledger_entries(pool_id, seq);

CREATE TABLE IF NOT EXISTS idempotency_records (
    scope          TEXT NOT NULL,
    partition      TEXT NOT NULL,
    idem_key       TEXT NOT NULL,
    request_hash   TEXT NOT NULL,
    status_code    INTEGER NOT NULL,
    response_body  TEXT NOT NULL,
    created_at_us  INTEGER NOT NULL,
    PRIMARY KEY (scope, partition, idem_key)
);
"""


def connect(db_path: str, busy_timeout_ms: int) -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        timeout=max(busy_timeout_ms / 1000.0, 0.1),
        isolation_level=None,  # autocommit mode: transactions are managed explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(settings: Settings) -> None:
    """Create schema (idempotent). Safe to run at every container start."""
    conn = connect(settings.db_path, settings.busy_timeout_ms)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serializing write transaction.

    ``BEGIN IMMEDIATE`` waits on the lock (up to busy_timeout) and ensures
    read-modify-write blocks commit atomically.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def fetch_pool(conn: sqlite3.Connection, pool_id: str, *, for_update: bool = False) -> sqlite3.Row | None:
    if for_update:
        # Inside an IMMEDIATE transaction the database lock is already held;
        # the row is stable for the rest of the transaction.
        return conn.execute("SELECT * FROM quota_pools WHERE id = ?", (pool_id,)).fetchone()
    return conn.execute("SELECT * FROM quota_pools WHERE id = ?", (pool_id,)).fetchone()


# ---------------------------------------------------------------------------
# Startup / request wiring
# ---------------------------------------------------------------------------

def init_for_startup(settings: Settings | None = None) -> None:
    from .services import bootstrap_default_pool, reconcile_all_pools

    settings = settings or load_settings()
    init_db(settings)
    conn = connect(settings.db_path, settings.busy_timeout_ms)
    try:
        if settings.init_default_pool:
            with write_tx(conn):
                bootstrap_default_pool(conn, "default", settings.default_pool_total, now_us())
        reconcile_all_pools(conn, raise_on_mismatch=False)
    finally:
        conn.close()


def get_conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request."""
    conn = connect(settings.db_path, settings.busy_timeout_ms)
    try:
        yield conn
    finally:
        conn.close()
