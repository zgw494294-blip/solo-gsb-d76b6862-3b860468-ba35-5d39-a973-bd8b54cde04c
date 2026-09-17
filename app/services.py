"""Domain services: reservations, settlement, expiry recycling, ledger.

Accounting model (invariant):

    available + held + used == total

Every state transition appends one ledger row whose three bucket deltas
sum to zero, so the invariant is preserved by construction::

    reserve   : available -= a, held += a            (-, +, 0)
    settle    : held -= a, available += a-u, used += u (a-u, -a, +u)
    release   : held -= a, available += a            (+, -, 0)
    expire    : held -= a, available += a            (+, -, 0)

A settled hold's unused portion (a - u) is refunded; settlement is only
legal while the reservation is ``held``, so it can succeed at most once.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from hashlib import sha256
from typing import Any, Callable

from .db import write_tx
from .errors import ApiError, not_found
from .timeutil import iso, now_us

logger = logging.getLogger("quota")

IDEM_SCOPE_RESERVE = "reservation.create"
IDEM_SCOPE_SETTLE = "reservation.settle"
IDEM_SCOPE_RELEASE = "reservation.release"
IDEM_SCOPE_POOL_CREATE = "pool.create"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def serialize_reservation(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "pool_id": r["pool_id"],
        "status": r["status"],
        "amount": r["amount"],
        "used_amount": r["used_amount"],
        "remaining_amount": r["amount"] - r["used_amount"],
        "expires_at": iso(r["expires_at_us"]),
        "expires_at_us": r["expires_at_us"],
        "created_at": iso(r["created_at_us"]),
        "finalized_at": iso(r["finalized_at_us"]) if r["finalized_at_us"] else None,
        "idempotency_key": r["idempotency_key"],
    }


def serialize_pool(p: sqlite3.Row, counts: dict[str, int]) -> dict[str, Any]:
    return {
        "id": p["id"],
        "total": p["total"],
        "available": p["available"],
        "held": p["held"],
        "used": p["used"],
        "reservations": counts,
        "created_at": iso(p["created_at_us"]),
    }


def serialize_ledger_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "seq": r["seq"],
        "pool_id": r["pool_id"],
        "reservation_id": r["reservation_id"],
        "type": r["type"],
        "amount": r["amount"],
        "delta_available": r["delta_available"],
        "delta_held": r["delta_held"],
        "delta_used": r["delta_used"],
        "created_at": iso(r["created_at_us"]),
    }


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def insert_ledger(
    conn: sqlite3.Connection,
    *,
    pool_id: str,
    type_: str,
    amount: int,
    delta_available: int,
    delta_held: int,
    delta_used: int,
    reservation_id: str | None = None,
    ts_us: int | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO ledger_entries
            (pool_id, reservation_id, type, amount,
             delta_available, delta_held, delta_used, created_at_us)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pool_id,
            reservation_id,
            type_,
            amount,
            delta_available,
            delta_held,
            delta_used,
            ts_us if ts_us is not None else now_us(),
        ),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Pools
# ---------------------------------------------------------------------------

def bootstrap_default_pool(
    conn: sqlite3.Connection, pool_id: str, total: int, ts_us: int
) -> bool:
    """Create the default pool if missing. Returns True when created."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO quota_pools (id, total, available, held, used, created_at_us)"
        " VALUES (?, ?, ?, 0, 0, ?)",
        (pool_id, total, total, ts_us),
    )
    if cur.rowcount:
        insert_ledger(
            conn,
            pool_id=pool_id,
            type_="pool_init",
            amount=total,
            delta_available=0,
            delta_held=0,
            delta_used=0,
            ts_us=ts_us,
        )
        logger.info("initialized pool %s with total=%d", pool_id, total)
        return True
    return False


def _create_pool_action(
    conn: sqlite3.Connection, pool_id: str, total: int, ts_us: int
) -> dict[str, Any]:
    existing = conn.execute(
        "SELECT id FROM quota_pools WHERE id = ?", (pool_id,)
    ).fetchone()
    if existing:
        raise ApiError(409, "pool_exists", f"pool {pool_id!r} already exists", pool_id=pool_id)
    conn.execute(
        "INSERT INTO quota_pools (id, total, available, held, used, created_at_us)"
        " VALUES (?, ?, ?, 0, 0, ?)",
        (pool_id, total, total, ts_us),
    )
    insert_ledger(
        conn,
        pool_id=pool_id,
        type_="pool_init",
        amount=total,
        delta_available=0,
        delta_held=0,
        delta_used=0,
        ts_us=ts_us,
    )
    p = conn.execute("SELECT * FROM quota_pools WHERE id = ?", (pool_id,)).fetchone()
    return serialize_pool(p, {"held": 0, "settled": 0, "released": 0, "expired": 0, "total": 0})


def create_pool(
    conn: sqlite3.Connection,
    pool_id: str,
    total: int,
    *,
    idem_key: str | None,
    request_hash: str,
    ts_us: int,
) -> tuple[int, dict[str, Any]]:
    if not idem_key:
        with write_tx(conn):
            return 201, _create_pool_action(conn, pool_id, total, ts_us)
    return _run_idempotent(
        conn,
        scope=IDEM_SCOPE_POOL_CREATE,
        partition=pool_id,
        key=idem_key,
        request_hash=request_hash,
        action=lambda c: (201, _create_pool_action(c, pool_id, total, ts_us)),
        ts_us=ts_us,
    )


def _pool_counts(conn: sqlite3.Connection, pool_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM reservations WHERE pool_id = ? GROUP BY status",
        (pool_id,),
    ).fetchall()
    by = {r["status"]: r["n"] for r in rows}
    counts = {k: int(by.get(k, 0)) for k in ("held", "settled", "released", "expired")}
    counts["total"] = sum(counts.values())
    return counts


def pool_status(conn: sqlite3.Connection, pool_id: str, ts_us: int) -> dict[str, Any]:
    with write_tx(conn):
        sweep_expired(conn, pool_id, ts_us)
        p = conn.execute("SELECT * FROM quota_pools WHERE id = ?", (pool_id,)).fetchone()
        if p is None:
            raise not_found("pool", pool_id)
        return serialize_pool(p, _pool_counts(conn, pool_id))


# ---------------------------------------------------------------------------
# Expiry recycling
# ---------------------------------------------------------------------------

def sweep_expired(conn: sqlite3.Connection, pool_id: str, ts_us: int) -> int:
    """Finalize all holds past their TTL and refund them to available.

    Runs at the start of every read and write so expired reservations are
    reclaimed on both access paths.
    """
    rows = conn.execute(
        "SELECT * FROM reservations"
        " WHERE pool_id = ? AND status = 'held' AND expires_at_us <= ?",
        (pool_id, ts_us),
    ).fetchall()
    for r in rows:
        amount = r["amount"]
        conn.execute(
            "UPDATE quota_pools SET available = available + ?, held = held - ? WHERE id = ?",
            (amount, amount, pool_id),
        )
        conn.execute(
            "UPDATE reservations SET status = 'expired', finalized_at_us = ? WHERE id = ?",
            (ts_us, r["id"]),
        )
        insert_ledger(
            conn,
            pool_id=pool_id,
            reservation_id=r["id"],
            type_="expire",
            amount=amount,
            delta_available=amount,
            delta_held=-amount,
            delta_used=0,
            ts_us=ts_us,
        )
        logger.info("reservation %s expired, %d units returned to pool %s",
                    r["id"], amount, pool_id)
    return len(rows)


def sweep_all_pools(conn: sqlite3.Connection, ts_us: int) -> int:
    ids = [r["id"] for r in conn.execute("SELECT id FROM quota_pools").fetchall()]
    return sum(sweep_expired(conn, pid, ts_us) for pid in ids)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def canonical_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return sha256(blob.encode("utf-8")).hexdigest()


def _replay(conn: sqlite3.Connection, scope: str, partition: str, key: str) -> tuple | None:
    row = conn.execute(
        "SELECT request_hash, status_code, response_body FROM idempotency_records"
        " WHERE scope = ? AND partition = ? AND idem_key = ?",
        (scope, partition, key),
    ).fetchone()
    if row is None:
        return None
    return row["request_hash"], int(row["status_code"]), json.loads(row["response_body"])


def _run_idempotent(
    conn: sqlite3.Connection,
    *,
    scope: str,
    partition: str,
    key: str,
    request_hash: str,
    action: Callable[[sqlite3.Connection], tuple[int, dict[str, Any]]],
    ts_us: int,
) -> tuple[int, dict[str, Any]]:
    # Fast path: serve the frozen original response without taking the write lock.
    cached = _replay(conn, scope, partition, key)
    if cached is not None:
        stored_hash, status_code, body = cached
        if stored_hash != request_hash:
            raise ApiError(
                409,
                "idempotency_key_conflict",
                "Idempotency-Key was already used with a different request payload",
            )
        return status_code, body

    # Serialized path. Another request with the same key may commit while we
    # wait on the lock, so the replay is repeated inside the transaction.
    conn.execute("BEGIN IMMEDIATE")
    try:
        cached = _replay(conn, scope, partition, key)
        if cached is not None:
            conn.execute("COMMIT")
            stored_hash, status_code, body = cached
            if stored_hash != request_hash:
                raise ApiError(409, "idempotency_key_conflict",
                               "Idempotency-Key was already used with a different request payload")
            return status_code, body

        try:
            status_code, body = action(conn)
        except ApiError as exc:
            # 422 = the request itself is invalid; deliberately do NOT freeze
            # it, so a corrected retry using the same key may succeed. The
            # transaction still commits to preserve sweep side effects.
            if exc.status_code == 422:
                conn.execute("COMMIT")
                raise
            # Freeze terminal errors (e.g. 402 insufficient quota, 404/409)
            # so retries observe the exact original outcome.
            _freeze(conn, scope, partition, key, request_hash, ts_us,
                    status_code=exc.status_code, body=exc.to_body())
            conn.execute("COMMIT")
            raise
        _freeze(conn, scope, partition, key, request_hash, ts_us,
                status_code=status_code, body=body)
        conn.execute("COMMIT")
        return status_code, body
    except ApiError:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _freeze(
    conn: sqlite3.Connection,
    scope: str,
    partition: str,
    key: str,
    request_hash: str,
    ts_us: int,
    *,
    status_code: int,
    body: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO idempotency_records"
        " (scope, partition, idem_key, request_hash, status_code, response_body, created_at_us)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (scope, partition, key, request_hash, status_code,
         json.dumps(body, ensure_ascii=False), ts_us),
    )


def raise_from_frozen(status_code: int, body: dict[str, Any]) -> None:
    """Re-raise a frozen error response with its original status and code."""
    err = body.get("error") or {}
    details = err.get("details") or {}
    raise ApiError(status_code, err.get("code", "idempotent_replay_error"),
                   err.get("message", "replayed error"), **details)


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------

def _get_held_reservation(
    conn: sqlite3.Connection, pool_id: str, reservation_id: str
) -> sqlite3.Row:
    r = conn.execute(
        "SELECT * FROM reservations WHERE id = ? AND pool_id = ?",
        (reservation_id, pool_id),
    ).fetchone()
    if r is None:
        raise not_found("reservation", reservation_id)
    if r["status"] != "held":
        raise ApiError(
            409,
            f"reservation_{r['status']}",
            f"reservation is already {r['status']}; operation not allowed",
            reservation_id=reservation_id,
        )
    return r


def _reserve_action(
    conn: sqlite3.Connection,
    pool_id: str,
    amount: int,
    ttl_seconds: int,
    idem_key: str,
    ts_us: int,
) -> tuple[int, dict[str, Any]]:
    p = conn.execute("SELECT * FROM quota_pools WHERE id = ?", (pool_id,)).fetchone()
    if p is None:
        raise not_found("pool", pool_id)
    sweep_expired(conn, pool_id, ts_us)
    p = conn.execute("SELECT * FROM quota_pools WHERE id = ?", (pool_id,)).fetchone()

    if amount > p["available"]:
        raise ApiError(
            402,
            "quota_exhausted",
            f"insufficient available quota: requested {amount}, available {p['available']}",
            requested=amount,
            available=p["available"],
        )

    reservation_id = uuid.uuid4().hex
    expires_at_us = ts_us + ttl_seconds * 1_000_000
    conn.execute(
        "UPDATE quota_pools SET available = available - ?, held = held + ? WHERE id = ?",
        (amount, amount, pool_id),
    )
    conn.execute(
        """
        INSERT INTO reservations
            (id, pool_id, amount, used_amount, status,
             expires_at_us, created_at_us, finalized_at_us, idempotency_key)
        VALUES (?, ?, ?, 0, 'held', ?, ?, NULL, ?)
        """,
        (reservation_id, pool_id, amount, expires_at_us, ts_us, idem_key),
    )
    seq = insert_ledger(
        conn,
        pool_id=pool_id,
        reservation_id=reservation_id,
        type_="reserve",
        amount=amount,
        delta_available=-amount,
        delta_held=amount,
        delta_used=0,
        ts_us=ts_us,
    )
    r = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
    body = serialize_reservation(r)
    body["ledger_seq"] = seq
    return 201, body


def create_reservation(
    conn: sqlite3.Connection,
    pool_id: str,
    amount: int,
    ttl_seconds: int,
    *,
    idem_key: str,
    ts_us: int,
) -> tuple[int, dict[str, Any]]:
    # Hash only the client-supplied inputs; the absolute expiry instant is
    # derived inside the transaction so concurrent retries hash identically.
    request_hash = canonical_hash({"amount": amount, "ttl_seconds": ttl_seconds})
    return _run_idempotent(
        conn,
        scope=IDEM_SCOPE_RESERVE,
        partition=pool_id,
        key=idem_key,
        request_hash=request_hash,
        action=lambda c: _reserve_action(c, pool_id, amount, ttl_seconds, idem_key, ts_us),
        ts_us=ts_us,
    )


def _settle_action(
    conn: sqlite3.Connection,
    pool_id: str,
    reservation_id: str,
    used_amount: int,
    ts_us: int,
) -> dict[str, Any]:
    sweep_expired(conn, pool_id, ts_us)
    r = _get_held_reservation(conn, pool_id, reservation_id)
    amount = r["amount"]
    if used_amount > amount:
        # Request validation failure: deliberately NOT frozen under the
        # idempotency key, so a corrected retry with the same key can succeed.
        raise ApiError(
            422,
            "usage_exceeds_hold",
            f"used_amount {used_amount} exceeds held amount {amount}",
            used_amount=used_amount,
            held_amount=amount,
        )

    refund = amount - used_amount
    conn.execute(
        "UPDATE quota_pools"
        " SET held = held - ?, available = available + ?, used = used + ?"
        " WHERE id = ?",
        (amount, refund, used_amount, pool_id),
    )
    conn.execute(
        "UPDATE reservations"
        " SET status = 'settled', used_amount = ?, finalized_at_us = ?"
        " WHERE id = ?",
        (used_amount, ts_us, reservation_id),
    )
    insert_ledger(
        conn,
        pool_id=pool_id,
        reservation_id=reservation_id,
        type_="settle",
        amount=used_amount,
        delta_available=refund,
        delta_held=-amount,
        delta_used=used_amount,
        ts_us=ts_us,
    )
    r = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
    p = conn.execute("SELECT * FROM quota_pools WHERE id = ?", (pool_id,)).fetchone()
    return {
        "reservation": serialize_reservation(r),
        "settled_usage": used_amount,
        "refunded": refund,
        "pool": serialize_pool(p, _pool_counts(conn, pool_id)),
    }


def settle_reservation(
    conn: sqlite3.Connection,
    pool_id: str,
    reservation_id: str,
    used_amount: int,
    *,
    idem_key: str,
    ts_us: int,
) -> dict[str, Any]:
    request_hash = canonical_hash({"used_amount": used_amount})
    status_code, body = _run_idempotent(
        conn,
        scope=IDEM_SCOPE_SETTLE,
        partition=reservation_id,
        key=idem_key,
        request_hash=request_hash,
        action=lambda c: (200, _settle_action(c, pool_id, reservation_id, used_amount, ts_us)),
        ts_us=ts_us,
    )
    if status_code != 200:
        raise_from_frozen(status_code, body)
    return body


def _release_action(
    conn: sqlite3.Connection, pool_id: str, reservation_id: str, ts_us: int
) -> dict[str, Any]:
    sweep_expired(conn, pool_id, ts_us)
    r = _get_held_reservation(conn, pool_id, reservation_id)
    amount = r["amount"]
    conn.execute(
        "UPDATE quota_pools SET held = held - ?, available = available + ? WHERE id = ?",
        (amount, amount, pool_id),
    )
    conn.execute(
        "UPDATE reservations SET status = 'released', finalized_at_us = ? WHERE id = ?",
        (ts_us, reservation_id),
    )
    insert_ledger(
        conn,
        pool_id=pool_id,
        reservation_id=reservation_id,
        type_="release",
        amount=amount,
        delta_available=amount,
        delta_held=-amount,
        delta_used=0,
        ts_us=ts_us,
    )
    r = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
    p = conn.execute("SELECT * FROM quota_pools WHERE id = ?", (pool_id,)).fetchone()
    return {
        "reservation": serialize_reservation(r),
        "refunded": amount,
        "pool": serialize_pool(p, _pool_counts(conn, pool_id)),
    }


def release_reservation(
    conn: sqlite3.Connection,
    pool_id: str,
    reservation_id: str,
    *,
    idem_key: str,
    ts_us: int,
) -> dict[str, Any]:
    request_hash = canonical_hash({"release": True})
    status_code, body = _run_idempotent(
        conn,
        scope=IDEM_SCOPE_RELEASE,
        partition=reservation_id,
        key=idem_key,
        request_hash=request_hash,
        action=lambda c: (200, _release_action(c, pool_id, reservation_id, ts_us)),
        ts_us=ts_us,
    )
    if status_code != 200:
        raise_from_frozen(status_code, body)
    return body


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_reservation(
    conn: sqlite3.Connection, pool_id: str, reservation_id: str, ts_us: int
) -> dict[str, Any]:
    with write_tx(conn):
        sweep_expired(conn, pool_id, ts_us)
        r = conn.execute(
            "SELECT * FROM reservations WHERE id = ? AND pool_id = ?",
            (reservation_id, pool_id),
        ).fetchone()
        if r is None:
            return None  # caller raises 404 after commit so the sweep is persisted
    return serialize_reservation(r)


def list_reservations(
    conn: sqlite3.Connection,
    pool_id: str,
    ts_us: int,
    *,
    status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    with write_tx(conn):
        p = conn.execute("SELECT id FROM quota_pools WHERE id = ?", (pool_id,)).fetchone()
        if p is None:
            raise not_found("pool", pool_id)
        sweep_expired(conn, pool_id, ts_us)
        if status:
            rows = conn.execute(
                "SELECT * FROM reservations WHERE pool_id = ? AND status = ?"
                " ORDER BY created_at_us, id LIMIT ?",
                (pool_id, status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reservations WHERE pool_id = ?"
                " ORDER BY created_at_us, id LIMIT ?",
                (pool_id, limit),
            ).fetchall()
    return {
        "pool_id": pool_id,
        "count": len(rows),
        "reservations": [serialize_reservation(r) for r in rows],
    }


def list_ledger(
    conn: sqlite3.Connection,
    pool_id: str,
    ts_us: int,
    *,
    after_seq: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    with write_tx(conn):
        p = conn.execute("SELECT id FROM quota_pools WHERE id = ?", (pool_id,)).fetchone()
        if p is None:
            raise not_found("pool", pool_id)
        sweep_expired(conn, pool_id, ts_us)
        rows = conn.execute(
            "SELECT * FROM ledger_entries WHERE pool_id = ? AND seq > ?"
            " ORDER BY seq ASC LIMIT ?",
            (pool_id, after_seq, limit + 1),
        ).fetchall()
    has_more = len(rows) > limit
    page = [serialize_ledger_row(r) for r in rows[:limit]]
    return {
        "pool_id": pool_id,
        "after_seq": after_seq,
        "limit": limit,
        "has_more": has_more,
        "next_after_seq": page[-1]["seq"] if page else after_seq,
        "entries": page,
    }


# ---------------------------------------------------------------------------
# Invariant verification
# ---------------------------------------------------------------------------

def reconcile_pool(conn: sqlite3.Connection, pool_id: str) -> dict[str, Any]:
    """Check the accounting invariant against the immutable ledger.

    * stored buckets must themselves satisfy available+held+used == total
    * buckets must equal total + sum(ledger deltas)
    * held must equal the sum of amounts over reservations still 'held'
    """
    p = conn.execute("SELECT * FROM quota_pools WHERE id = ?", (pool_id,)).fetchone()
    if p is None:
        raise not_found("pool", pool_id)
    agg = conn.execute(
        "SELECT COALESCE(SUM(delta_available),0) AS da,"
        " COALESCE(SUM(delta_held),0) AS dh,"
        " COALESCE(SUM(delta_used),0) AS du,"
        " COUNT(*) AS n"
        " FROM ledger_entries WHERE pool_id = ?",
        (pool_id,),
    ).fetchone()
    active = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS held_sum, COUNT(*) AS held_n"
        " FROM reservations WHERE pool_id = ? AND status = 'held'",
        (pool_id,),
    ).fetchone()

    expected_available = p["total"] + agg["da"]
    expected_held = agg["dh"]
    expected_used = agg["du"]
    checks = {
        "buckets_sum_to_total": p["available"] + p["held"] + p["used"] == p["total"],
        "available_matches_ledger": p["available"] == expected_available,
        "held_matches_ledger": p["held"] == expected_held,
        "used_matches_ledger": p["used"] == expected_used,
        "held_matches_active_reservations": p["held"] == active["held_sum"],
        "deltas_balanced": agg["da"] + agg["dh"] + agg["du"] == 0,
    }
    return {
        "pool_id": pool_id,
        "balanced": all(checks.values()),
        "checks": checks,
        "stored": {
            "total": p["total"],
            "available": p["available"],
            "held": p["held"],
            "used": p["used"],
        },
        "from_ledger": {
            "available": expected_available,
            "held": expected_held,
            "used": expected_used,
            "entries": agg["n"],
        },
        "active_reservations": {"count": active["held_n"], "held_sum": active["held_sum"]},
    }


def reconcile_all_pools(conn: sqlite3.Connection, raise_on_mismatch: bool = False) -> list[dict[str, Any]]:
    ids = [r["id"] for r in conn.execute("SELECT id FROM quota_pools").fetchall()]
    results = []
    for pid in ids:
        report = reconcile_pool(conn, pid)
        results.append(report)
        if not report["balanced"]:
            logger.error("invariant violation for pool %s: %s", pid, report["checks"])
            if raise_on_mismatch:
                raise ApiError(500, "invariant_violation",
                               "pool accounting invariant check failed", pool_id=pid)
    return results
