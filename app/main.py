"""FastAPI application bootstrap with channel webhook endpoints."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.adapters.base import ChannelAdapterError
from app.adapters.feishu import FeishuAdapter
from app.adapters.wecom import WeComAdapter
from app.application.context_builder import ContextBuilder
from app.application.identity_service import IdentityResolver
from app.application.ingress_service import IngressService
from app.application.intent_router import IntentRouter
from app.application.retriever import Retriever
from app.application.session_service import SessionService
from app.application.support_agent import SupportAgent
from app.application.ticket_service import TicketResolver, TicketService
from app.application.workflow import SupportWorkflow
from app.domain.envelope import InboundEnvelope
from app.domain.identity import Session, User
from app.infrastructure.db import apply_migrations, connect
from app.infrastructure.idempotency import IdempotencyStore
from app.infrastructure.llm import LLMClient, llm_client_from_env
from app.infrastructure.repositories import (
    ChannelIdentityRepository,
    MessageRepository,
    SessionRepository,
    TicketStore,
    UserRepository,
)

_SEED_DIR = Path(__file__).resolve().parent.parent / "seed" / "faq"


def build_workflow(
    conn: Any,
    store: TicketStore,
    seed_dir: str | Path = _SEED_DIR,
    llm: LLMClient | None = None,
) -> SupportWorkflow:
    """Assemble the Phase 4 workflow (intent -> RAG or ticket -> agent)."""
    return SupportWorkflow(
        router=IntentRouter(),
        retriever=Retriever(seed_dir),
        ticket_service=TicketService(store),
        resolver=TicketResolver(TicketService(store), store),
        context_builder=ContextBuilder(MessageRepository(conn)),
        agent=SupportAgent(llm=llm),
        messages=MessageRepository(conn),
    )


def build_ingress(
    db_path: str = ":memory:",
    downstream: Callable[[InboundEnvelope, User, Session], object] | None = None,
    seed_dir: str | Path = _SEED_DIR,
    llm: LLMClient | None = None,
) -> tuple[IngressService, Any, TicketStore]:
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
        downstream=downstream or build_workflow(conn, store, seed_dir=seed_dir, llm=llm).handle,
    ), conn, store


def create_app(ingress: IngressService | None = None) -> FastAPI:
    """App factory; webhook endpoints registered here.

    When no ingress is injected (module-level app), one is built lazily
    on first webhook request with a default runtime DB path.
    """
    app = FastAPI(
        title="support-agent-lite",
        version="0.1.0",
        description="Cross-channel enterprise support agent (user-centric, workflow-first).",
    )
    app.state.ingress = ingress

    def _ingress() -> IngressService:
        if app.state.ingress is None:
            import os

            default_db = os.environ.get("SUPPORT_AGENT_DB", "runtime/support_agent.db")
            app.state.ingress, _, _ = build_ingress(db_path=default_db, seed_dir=_SEED_DIR, llm=llm_client_from_env())
        return app.state.ingress

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    def root() -> dict[str, str]:
        return {"service": "support-agent-lite", "docs": "/docs"}

    @app.post("/webhooks/{channel}")
    async def webhook(channel: str, request: Request) -> JSONResponse:
        try:
            payload: dict[str, Any] = await request.json()
            result = _ingress().process(channel, payload)
        except ChannelAdapterError as exc:
            raise HTTPException(status_code=400, detail=f"{exc.code}: {exc}") from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown channel: {channel}") from exc

        status_code = 200 if not result.duplicate else 202
        downstream = result.downstream
        return JSONResponse(
            status_code=status_code,
            content={
                "ok": True,
                "duplicate": result.duplicate,
                "trace_id": result.envelope.trace_id,
                "user_id": result.user.id,
                "session_id": result.session.id,
                "workflow": getattr(downstream, "kind", None),
                "ticket_id": getattr(downstream, "ticket", None) and downstream.ticket.id,
                "reply": getattr(downstream, "reply", None),
            },
        )

    return app


app = create_app()