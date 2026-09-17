"""TTL expiry recycling on both read and write paths."""
from __future__ import annotations

import time


def test_expired_hold_recycled_on_status_query(client, pool):
    r = client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 300, "ttl_seconds": 1},
        headers={"Idempotency-Key": "exp1"},
    )
    rid = r.json()["id"]
    assert client.get(f"/pools/{pool}/status").json()["held"] == 300

    time.sleep(1.2)

    # A READ triggers recycling.
    st = client.get(f"/pools/{pool}/status").json()
    assert st["held"] == 0
    assert st["available"] == 1000
    assert st["reservations"]["expired"] == 1

    detail = client.get(f"/pools/{pool}/reservations/{rid}").json()
    assert detail["status"] == "expired"
    assert detail["finalized_at"] is not None

    # Expire ledger entry exists and is balanced.
    led = client.get(f"/pools/{pool}/ledger", params={"limit": 500}).json()
    expire = [e for e in led["entries"] if e["type"] == "expire"]
    assert len(expire) == 1
    assert expire[0]["delta_available"] == 300
    assert expire[0]["delta_held"] == -300


def test_settle_after_expiry_rejected_and_funds_returned(client, pool):
    rid = client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 100, "ttl_seconds": 1},
        headers={"Idempotency-Key": "exp2"},
    ).json()["id"]
    time.sleep(1.2)
    r = client.post(
        f"/pools/{pool}/reservations/{rid}/settle",
        json={"used_amount": 1},
        headers={"Idempotency-Key": "late"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "reservation_expired"
    st = client.get(f"/pools/{pool}/status").json()
    assert st["held"] == 0 and st["available"] == 1000 and st["used"] == 0


def test_write_path_also_recycles(client, pool):
    client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 950, "ttl_seconds": 1},
        headers={"Idempotency-Key": "w1"},
    )
    time.sleep(1.2)
    # Only 50 available until the expired 950 is recycled by this very write.
    r = client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 900},
        headers={"Idempotency-Key": "w2"},
    )
    assert r.status_code == 201, r.text
    st = client.get(f"/pools/{pool}/status").json()
    assert st["available"] == 100 and st["held"] == 900


def test_release_after_expiry_is_idempotent_409(client, pool):
    rid = client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 10, "ttl_seconds": 1},
        headers={"Idempotency-Key": "e3"},
    ).json()["id"]
    time.sleep(1.2)
    h = {"Idempotency-Key": "late-rel"}
    r1 = client.post(f"/pools/{pool}/reservations/{rid}/release", headers=h)
    r2 = client.post(f"/pools/{pool}/reservations/{rid}/release", headers=h)
    assert r1.status_code == 409 and r2.status_code == 409
    assert r1.json()["error"]["code"] == "reservation_expired"
