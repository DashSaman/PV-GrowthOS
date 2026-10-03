"""Phase 0 acceptance: FastAPI starts, /health and /ready work, DB reachable."""


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["app"] == "pv-growth"


def test_ready(client):
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_ready_503_when_db_down(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from pv_growth.main import create_app

    broken = type("S", (), {"database_url": "sqlite+pysqlite:///./nonexistent-dir-xyz/db.db"})()
    monkeypatch.setattr("pv_growth.api.health.get_settings", lambda: broken)
    with TestClient(create_app()) as c:
        resp = c.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "not-ready"
