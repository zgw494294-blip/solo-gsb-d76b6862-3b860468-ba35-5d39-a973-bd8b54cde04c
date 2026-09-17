"""Core reservation lifecycle, refunds and accounting invariant."""
from __future__ import annotations


def test_health_and_default_pool(client):
    assert client.get("/health").json() == {"status": "ok"}
    st = client.get("/pools/default/status").json()
    assert st["total"] == 1000
    assert st["available"] == 1000
    assert st["held"] == 0
    assert st["used"] == 0


def test_pool_must_exist(client):
    r = client.post(
        "/pools/nope/reservations",
        json={"amount": 10},
        headers={"Idempotency-Key": "k1"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "pool_not_found"


def test_missing_idempotency_key_rejected(client, pool):
    r = client.post(f"/pools/{pool}/reservations", json={"amount": 10})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "missing_idempotency_key"


def test_reserve_settle_with_partial_usage_refund(client, pool):
    r = client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 100},
        headers={"Idempotency-Key": "resv-1"},
    )
    assert r.status_code == 201, r.text
    resv = r.json()
    assert resv["status"] == "held"
    assert resv["amount"] == 100

    st = client.get(f"/pools/{pool}/status").json()
    assert st["available"] == 900 and st["held"] == 100 and st["used"] == 0
    assert st["reservations"]["held"] == 1

    s = client.post(
        f"/pools/{pool}/reservations/{resv['id']}/settle",
        json={"used_amount": 40},
        headers={"Idempotency-Key": "set-1"},
    )
    assert s.status_code == 200, s.text
    body = s.json()
    assert body["settled_usage"] == 40
    assert body["refunded"] == 60
    assert body["reservation"]["status"] == "settled"
    assert body["reservation"]["used_amount"] == 40

    st = client.get(f"/pools/{pool}/status").json()
    # total = available(960) + held(0) + used(40)
    assert (st["available"], st["held"], st["used"]) == (960, 0, 40)
    assert st["available"] + st["held"] + st["used"] == st["total"]


def test_release_refunds_full_amount(client, pool):
    r = client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 250, "ttl_seconds": 60},
        headers={"Idempotency-Key": "resv-rel"},
    )
    rid = r.json()["id"]
    rel = client.post(
        f"/pools/{pool}/reservations/{rid}/release",
        headers={"Idempotency-Key": "rel-1"},
    )
    assert rel.status_code == 200
    assert rel.json()["refunded"] == 250
    st = client.get(f"/pools/{pool}/status").json()
    assert (st["available"], st["held"], st["used"]) == (1000, 0, 0)


def test_settle_twice_only_first_succeeds(client, pool):
    rid = client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 100},
        headers={"Idempotency-Key": "twice"},
    ).json()["id"]
    s1 = client.post(
        f"/pools/{pool}/reservations/{rid}/settle",
        json={"used_amount": 30},
        headers={"Idempotency-Key": "s1"},
    )
    assert s1.status_code == 200
    s2 = client.post(
        f"/pools/{pool}/reservations/{rid}/settle",
        json={"used_amount": 30},
        headers={"Idempotency-Key": "s2"},
    )
    assert s2.status_code == 409
    assert s2.json()["error"]["code"] == "reservation_settled"

    rel = client.post(
        f"/pools/{pool}/reservations/{rid}/release",
        headers={"Idempotency-Key": "r2"},
    )
    assert rel.status_code == 409


def test_usage_cannot_exceed_hold(client, pool):
    rid = client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 50},
        headers={"Idempotency-Key": "over"},
    ).json()["id"]
    r = client.post(
        f"/pools/{pool}/reservations/{rid}/settle",
        json={"used_amount": 51},
        headers={"Idempotency-Key": "sover"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "usage_exceeds_hold"
    # Hold must remain intact after the rejected settle.
    detail = client.get(f"/pools/{pool}/reservations/{rid}").json()
    assert detail["status"] == "held"

    # A corrected retry with the SAME key must be allowed (validation errors
    # are not frozen).
    r2 = client.post(
        f"/pools/{pool}/reservations/{rid}/settle",
        json={"used_amount": 50},
        headers={"Idempotency-Key": "sover"},
    )
    assert r2.status_code == 200
    assert r2.json()["settled_usage"] == 50


def test_cannot_overcommit(client, pool):
    r1 = client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 800},
        headers={"Idempotency-Key": "a"},
    )
    assert r1.status_code == 201
    r2 = client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 300},
        headers={"Idempotency-Key": "b"},
    )
    assert r2.status_code == 402
    assert r2.json()["error"]["code"] == "quota_exhausted"
    assert r2.json()["error"]["details"]["available"] == 200


def test_ttl_validation(client, pool):
    r = client.post(
        f"/pools/{pool}/reservations",
        json={"amount": 1, "ttl_seconds": 99999},
        headers={"Idempotency-Key": "bigttl"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ttl_too_large"


def test_verify_endpoint_balanced(client, pool):
    client.post(f"/pools/{pool}/reservations",
                json={"amount": 100}, headers={"Idempotency-Key": "v"})
    rep = client.get(f"/pools/{pool}/verify").json()
    assert rep["balanced"] is True
    assert all(rep["checks"].values())
