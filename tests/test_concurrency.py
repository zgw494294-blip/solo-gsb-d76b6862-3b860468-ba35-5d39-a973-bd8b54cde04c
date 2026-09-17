"""Concurrency: writers serialize through BEGIN IMMEDIATE; total holds must
never exceed pool availability, and concurrent identical idempotency keys
must produce exactly one reservation.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from app.config import load_settings
from app.db import connect, init_for_startup
from app import services
from app.timeutil import now_us


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTA_DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setenv("QUOTA_POOL_TOTAL", "0")  # no default pool traffic
    monkeypatch.setenv("QUOTA_INIT_DEFAULT_POOL", "false")
    settings = load_settings()
    init_for_startup(settings)
    conn = connect(settings.db_path, settings.busy_timeout_ms)
    from app.db import write_tx

    with write_tx(conn):
        services._create_pool_action(conn, "p", 1000, now_us())
    conn.close()
    return settings


def test_parallel_reservations_never_overcommit(db_env):
    total_threads = 40
    amount = 100  # pool of 1000 => at most 10 can succeed

    results: list[tuple[int, dict]] = []
    lock = threading.Lock()

    def worker(i: int):
        conn = connect(db_env.db_path, db_env.busy_timeout_ms)
        try:
            out = services.create_reservation(
                conn,
                pool_id="p",
                amount=amount,
                ttl_seconds=3600,
                idem_key=f"key-{i}",
                ts_us=now_us(),
            )
            with lock:
                results.append(out)
        except Exception as exc:  # noqa: BLE001
            with lock:
                results.append((getattr(exc, "status_code", 500), {}))
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(worker, range(total_threads)))

    successes = [r for r in results if r[0] == 201]
    rejected = [r for r in results if r[0] == 402]
    assert len(successes) == 10
    assert len(rejected) == 30
    ids = {r[1]["id"] for r in successes}
    assert len(ids) == 10

    conn = connect(db_env.db_path, db_env.busy_timeout_ms)
    try:
        report = services.reconcile_pool(conn, "p")
        assert report["balanced"] is True, report
        assert report["stored"]["held"] == 1000
        assert report["stored"]["available"] == 0
        assert report["active_reservations"]["count"] == 10
    finally:
        conn.close()


def test_concurrent_same_idempotency_key_creates_once(db_env):
    barrier = threading.Barrier(8)
    outcomes: list[tuple[int, str | None]] = []
    lock = threading.Lock()

    def worker():
        conn = connect(db_env.db_path, db_env.busy_timeout_ms)
        barrier.wait()
        try:
            code, body = services.create_reservation(
                conn,
                pool_id="p",
                amount=100,
                ttl_seconds=3600,
                idem_key="same-key",
                ts_us=now_us(),
            )
            with lock:
                outcomes.append((code, body.get("id")))
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(worker) for _ in range(8)]
        for f in as_completed(futures):
            f.result()

    assert len(outcomes) == 8
    assert all(code == 201 for code, _ in outcomes)
    assert {rid for _, rid in outcomes} == {outcomes[0][1]}

    conn = connect(db_env.db_path, db_env.busy_timeout_ms)
    try:
        report = services.reconcile_pool(conn, "p")
        assert report["balanced"] is True
        assert report["stored"]["held"] == 100
        assert report["active_reservations"]["count"] == 1
    finally:
        conn.close()


def test_concurrent_settle_and_release_only_one_finalizes(db_env):
    conn = connect(db_env.db_path, db_env.busy_timeout_ms)
    _, body = services.create_reservation(
        conn, "p", 100, 3600,
        idem_key="held-key", ts_us=now_us(),
    )
    rid = body["id"]
    conn.close()

    errors = []

    def settle():
        c = connect(db_env.db_path, db_env.busy_timeout_ms)
        try:
            services.settle_reservation(
                c, "p", rid, used_amount=60, idem_key="sk", ts_us=now_us()
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(("settle", getattr(exc, "code", type(exc).__name__)))
        finally:
            c.close()

    def release():
        c = connect(db_env.db_path, db_env.busy_timeout_ms)
        try:
            services.release_reservation(c, "p", rid, idem_key="rk", ts_us=now_us())
        except Exception as exc:  # noqa: BLE001
            errors.append(("release", getattr(exc, "code", type(exc).__name__)))
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(settle)
        f2 = ex.submit(release)
        f1.result(); f2.result()

    assert len(errors) == 1
    assert errors[0][1] in {"reservation_settled", "reservation_released"}

    c = connect(db_env.db_path, db_env.busy_timeout_ms)
    try:
        report = services.reconcile_pool(c, "p")
        assert report["balanced"] is True, report
        if errors[0][0] == "release":
            assert (report["stored"]["held"], report["stored"]["used"]) == (0, 60)
        else:
            assert (report["stored"]["held"], report["stored"]["used"]) == (0, 0)
    finally:
        c.close()
