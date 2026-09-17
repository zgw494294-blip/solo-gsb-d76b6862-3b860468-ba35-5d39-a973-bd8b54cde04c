"""Idempotency semantics for create / settle / release."""
from __future__ import annotations


def test_create_reservation_replay_returns_original(client, pool):
    headers = {"Idempotency-Key": "idem-create-1"}
    r1 = client.post(f"/pools/{pool}/reservations", json={"amount": 100}, headers=headers)
    r2 = client.post(f"/pools/{pool}/reservations", json={"amount": 100}, headers=headers)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]
    # Only one reservation / one reserve ledger row must exist.
    st = client.get(f"/pools/{pool}/status").json()
    assert st["held"] == 100
    led = client.get(f"/pools/{pool}/ledger", params={"limit": 500}).json()
    reserve_rows = [e for e in led["entries"] if e["type"] == "reserve"]
    assert len(reserve_rows) == 1


def test_same_key_different_payload_conflicts(client, pool):
    client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 100, "ttl_seconds": 60},
        headers={"Idempotency-Key": "dup"},
    )
    r = client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 200, "ttl_seconds": 60},
        headers={"Idempotency-Key": "dup"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_key_conflict"


def test_insufficient_quota_response_is_replayed(client, pool):
    r1 = client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 2000},
        headers={"Idempotency-Key": "poor"},
    )
    r2 = client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 2000},
        headers={"Idempotency-Key": "poor"},
    )
    assert r1.status_code == 402 and r2.status_code == 402
    assert r1.json() == r2.json()
    st = client.get(f"/pools/{pool}/status").json()
    assert st["available"] == 1000 and st["held"] == 0


def test_settle_and_release_idempotent(client, pool):
    rid = client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 100},
        headers={"Idempotency-Key": "hold"},
    ).json()["id"]

    s1 = client.post(
        f"/pools/{pool}/reservations/{rid}/settle",
        json={"used_amount": 25},
        headers={"Idempotency-Key": "settle"},
    )
    s2 = client.post(
        f"/pools/{pool}/reservations/{rid}/settle",
        json={"used_amount": 25},
        headers={"Idempotency-Key": "settle"},
    )
    assert s1.status_code == 200 and s2.status_code == 200
    assert s1.json() == s2.json()

    # A release on an already-settled reservation is replayed identically.
    rel1 = client.post(
        f"/pools/{pool}/reservations/{rid}/release",
        headers={"Idempotency-Key": "rel"},
    )
    rel2 = client.post(
        f"/pools/{pool}/reservations/{rid}/release",
        headers={"Idempotency-Key": "rel"},
    )
    assert rel1.status_code == 409 and rel2.status_code == 409
    assert rel1.json() == rel2.json()

    led = client.get(f"/pools/{pool}/ledger", params={"limit": 500}).json()
    types = [e["type"] for e in led["entries"]]
    assert types.count("settle") == 1
    assert types.count("release") == 0


def test_keys_scoped_per_pool_and_operation(client, pool):
    client.put("/pools/p2", json={"total": 500})
    h = {"Idempotency-Key": "scoped"}
    r1 = client.post("/pools/p1/reservations", json={"amount": 10}, headers=h)
    r2 = client.post("/pools/p2/reservations", json={"amount": 20}, headers=h)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]
