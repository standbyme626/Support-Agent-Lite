"""FastAPI application bootstrap with channel webhook endpoints."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.adapters.base import ChannelAdapterError
from app.adapters.feishu import FeishuAdapter
from app.adapters.wecom import WeComAdapter
from app.application.identity_service import IdentityResolver
from app.application.ingress_service import IngressService
from app.application.session_service import SessionService
from app.domain.envelope import InboundEnvelope
from app.domain.identity import Session, User
from app.infrastructure.db import apply_migrations, connect
from app.infrastructure.idempotency import IdempotencyStore
from app.infrastructure.repositories import (
    ChannelIdentityRepository,
    SessionRepository,
    TicketStore,
    UserRepository,
)


def build_ingress(
    db_path: str = ":memory:",
    downstream: Callable[[InboundEnvelope, User, Session], object] | None = None,
) -> IngressService:
    """Assemble the ingress pipeline (adapters + identity + sessions + idempotency)."""
    conn = connect(db_path)
    apply_migrations(conn)
    users = UserRepository(conn)
    identities = ChannelIdentityRepository(conn)
    sessions = SessionRepository(conn)
    store = TicketStore(conn)
    return IngressService(
        adapters={"wecom": WeComAdapter(), "feishu": FeishuAdapter()},
        identity=IdentityResolver(users, identities),
        sessions=SessionService(sessions),
        idempotency=IdempotencyStore(conn),
        downstream=downstream,
    ), conn, store


def create_app(ingress: IngressService | None = None) -> FastAPI:
    """App factory; webhook endpoints registered here."""
    app = FastAPI(
        title="support-agent-lite",
        version="0.1.0",
        description="Cross-channel enterprise support agent (user-centric, workflow-first).",
    )
    app.state.ingress = ingress

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    def root() -> dict[str, str]:
        return {"service": "support-agent-lite", "docs": "/docs"}

    @app.post("/webhooks/{channel}")
    async def webhook(channel: str, request: Request) -> JSONResponse:
        if app.state.ingress is None:
            raise HTTPException(status_code=503, detail="ingress not configured")
        try:
            payload: dict[str, Any] = await request.json()
            result = app.state.ingress.process(channel, payload)
        except ChannelAdapterError as exc:
            raise HTTPException(status_code=400, detail=f"{exc.code}: {exc}") from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown channel: {channel}") from exc

        status_code = 200 if not result.duplicate else 202
        return JSONResponse(
            status_code=status_code,
            content={
                "ok": True,
                "duplicate": result.duplicate,
                "trace_id": result.envelope.trace_id,
                "user_id": result.user.id,
                "session_id": result.session.id,
            },
        )

    return app


app = create_app()