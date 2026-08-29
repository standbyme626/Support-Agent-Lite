"""Startup warm-up: the runtime must be built when the port opens, not on
the first inbound message (P0.5 cold-start fix, 2026-08-29)."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_startup_prebuilds_runtime(monkeypatch):
    """create_app() without injected services builds everything at startup,
    so app.state.ingress is populated before the first request is served."""
    monkeypatch.setenv("SUPPORT_AGENT_DB", ":memory:")
    from app.main import create_app

    app = create_app()
    assert app.state.ingress is None  # lazy until the lifespan starts

    with TestClient(app) as client:  # context manager fires startup hooks
        resp = client.get("/health")
        assert resp.status_code == 200
        assert app.state.ingress is not None
        assert app.state.ops is not None


def test_startup_with_injected_services_skips_warmup(app_ctx):
    """Injected apps (test fixture path) must not rebuild anything."""
    from app.main import create_app

    app = create_app(app_ctx.ingress, None)
    assert app.state.ingress is app_ctx.ingress
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert app.state.ingress is app_ctx.ingress  # same instance, no rebuild
