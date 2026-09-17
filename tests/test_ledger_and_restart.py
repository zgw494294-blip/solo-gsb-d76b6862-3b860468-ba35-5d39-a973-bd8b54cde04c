"""Immutable ledger, pagination and persistence across restarts."""
from __future__ import annotations

import os
import sqlite3

import pytest


def test_ledger_balances_and_pagination(client, pool):
    rid = client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 100},
        headers={"Idempotency-Key": "p"},
    ).json()["id"]
    client.post(
        f"/pools/{pool}/reservations/{rid}/settle",
        json={"used_amount": 70},
        headers={"Idempotency-Key": "ps"},
    )
    client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 50},
        headers={"Idempotency-Key": "p2"},
    )

    page1 = client.get(f"/pools/{pool}/ledger", params={"limit": 2}).json()
    assert len(page1["entries"]) == 2
    assert page1["has_more"] is True
    cursor = page1["next_after_seq"]
    seqs = [e["seq"] for e in page1["entries"]]
    assert seqs == sorted(seqs)

    page2 = client.get(
        f"/pools/{pool}/ledger", params={"limit": 2, "after_seq": cursor}
    ).json()
    assert all(e["seq"] > cursor for e in page2["entries"])

    full = client.get(
        f"/pools/{pool}/ledger", params={"limit": 500}
    ).json()["entries"]
    # Every ledger row keeps the buckets balanced.
    for e in full:
        assert e["delta_available"] + e["delta_held"] + e["delta_used"] == 0

    # Direct SQL: UPDATE/DELETE must be rejected by triggers.
    db_path = os.environ["QUOTA_DB_PATH"]
    raw = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("UPDATE ledger_entries SET amount = 0 WHERE seq = 1")
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("DELETE FROM ledger_entries WHERE seq = 1")
    finally:
        raw.close()


def test_state_survives_restart(tmp_path, monkeypatch):
    db_path = str(tmp_path / "restart.db")
    monkeypatch.setenv("QUOTA_DB_PATH", db_path)
    monkeypatch.setenv("QUOTA_POOL_TOTAL", "500")
    monkeypatch.setenv("QUOTA_INIT_DEFAULT_POOL", "true")
    monkeypatch.setenv("QUOTA_DEFAULT_TTL_SECONDS", "3600")

    import importlib
    import app.config
    import app.db
    import app.main
    import app.services
    for mod in (app.config, app.db, app.services, app.main):
        importlib.reload(mod)
    from fastapi.testclient import TestClient

    app.main.settings = app.config.load_settings()
    app.db.init_for_startup(app.main.settings)

    with TestClient(app.main.app) as c:
        rid = c.post(
            "/pools/default/reservations",
            json={"amount": 200},
            headers={"Idempotency-Key": "k-before"},
        ).json()["id"]
        c.post(
            f"/pools/default/reservations/{rid}/settle",
            json={"used_amount": 120},
            headers={"Idempotency-Key": "s-before"},
        )
        c.post(
            "/pools/default/reservations",
            json={"amount": 100},
            headers={"Idempotency-Key": "k-held"},
        )

    # Simulate container restart: rebuild everything from the same DB file.
    for mod in (app.config, app.db, app.services, app.main):
        importlib.reload(mod)
    app.main.settings = app.config.load_settings()
    app.db.init_for_startup(app.main.settings)  # invariant check at boot

    with TestClient(app.main.app) as c2:
        st = c2.get("/pools/default/status").json()
        assert st["total"] == 500
        assert st["used"] == 120
        assert st["held"] == 100
        assert st["available"] == 280
        rep = c2.get("/pools/default/verify").json()
        assert rep["balanced"] is True, rep
        # Idempotency memory persisted: replaying the first key returns the
        # original reservation.
        replay = c2.post(
            "/pools/default/reservations",
            json={"amount": 200},
            headers={"Idempotency-Key": "k-before"},
        )
        assert replay.status_code == 201
        assert replay.json()["id"] == rid
