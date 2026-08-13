"""Phase 0 tests: app startup and health endpoint."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_app_starts() -> None:
    assert app.title == "support-agent-lite"


def test_health_endpoint() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_root_endpoint() -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["service"] == "support-agent-lite"
