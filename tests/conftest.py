"""Shared pytest fixtures: isolated temp DB + TestClient."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_quota.db")
    monkeypatch.setenv("QUOTA_DB_PATH", db_path)
    monkeypatch.setenv("QUOTA_POOL_TOTAL", "1000")
    monkeypatch.setenv("QUOTA_INIT_DEFAULT_POOL", "true")
    monkeypatch.setenv("QUOTA_DEFAULT_TTL_SECONDS", "2")
    monkeypatch.setenv("QUOTA_MAX_TTL_SECONDS", "3600")

    import app.config
    import app.db
    import app.main
    import app.services
    importlib.reload(app.config)
    importlib.reload(app.db)
    importlib.reload(app.services)
    importlib.reload(app.main)

    from fastapi.testclient import TestClient

    app.main.settings = app.config.load_settings()
    app.db.init_for_startup(app.main.settings)
    with TestClient(app.main.app) as c:
        yield c


@pytest.fixture()
def pool(client):
    resp = client.put("/pools/p1", json={"total": 1000})
    assert resp.status_code in (201,), resp.text
    return "p1"
