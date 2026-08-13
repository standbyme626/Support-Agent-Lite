"""FastAPI application bootstrap with channel webhook and operator endpoints."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.adapters.base import ChannelAdapterError
from app.adapters.feishu import FeishuAdapter
from app.adapters.wecom import WeComAdapter
from app.application.approval_service import ApprovalService
from app.application.context_builder import ContextBuilder
from app.application.identity_service import IdentityResolver
from app.application.ingress_service import IngressService
from app.application.intent_router import IntentRouter
from app.application.retriever import Retriever
from app.application.session_service import SessionService
from app.application.support_agent import SupportAgent
from app.application.ticket_service import TicketResolver, TicketService
from app.application.workflow import SupportWorkflow
from app.domain.approval import ApprovalStatus, InvalidApprovalDecision
from app.domain.envelope import InboundEnvelope
from app.domain.identity import Session, User
from app.domain.ticket import InvalidStateTransition
from app.infrastructure.db import apply_migrations, connect
from app.infrastructure.idempotency import IdempotencyStore
from app.infrastructure.llm import LLMClient, llm_client_from_env
from app.infrastructure.repositories import (
    ApprovalRepository,
    ChannelIdentityRepository,
    MessageRepository,
    SessionRepository,
    TicketStore,
    UserRepository,
)

_SEED_DIR = Path(__file__).resolve().parent.parent / "seed" / "faq"


@dataclass
class OpsServices:
    """Operator-facing services (tickets + independent approvals)."""

    tickets: TicketService
    approvals: ApprovalService


def build_ops(conn: Any, store: TicketStore) -> OpsServices:
    """Assemble operator + approval services on the same DB connection."""
    return OpsServices(
        tickets=TicketService(store),
        approvals=ApprovalService(store, ApprovalRepository(conn)),
    )


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


def create_app(ingress: IngressService | None = None, ops: OpsServices | None = None) -> FastAPI:
    """App factory; webhook and operator endpoints registered here.

    When no services are injected (module-level app), both are built
    lazily on first request with a default runtime DB path.
    """
    app = FastAPI(
        title="support-agent-lite",
        version="0.1.0",
        description="Cross-channel enterprise support agent (user-centric, workflow-first).",
    )
    app.state.ingress = ingress
    app.state.ops = ops

    def _services() -> None:
        if app.state.ingress is None:
            import os

            default_db = os.environ.get("SUPPORT_AGENT_DB", "runtime/support_agent.db")
            ingress, conn, store = build_ingress(
                db_path=default_db, seed_dir=_SEED_DIR, llm=llm_client_from_env()
            )
            app.state.ingress = ingress
            app.state.ops = build_ops(conn, store)

    def _ingress() -> IngressService:
        _services()
        return app.state.ingress

    def _ops() -> OpsServices:
        _services()
        if app.state.ops is None:
            raise HTTPException(status_code=503, detail="ops not configured")
        return app.state.ops

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

    # --- Operator API (Phase 5: human collaboration + HITL) ---

    @app.post("/tickets/{ticket_id}/claim")
    def claim(ticket_id: str) -> Any:
        try:
            return _ops().tickets.claim(ticket_id)
        except InvalidStateTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/tickets/{ticket_id}/resolve")
    def resolve(ticket_id: str, payload: dict | None = None) -> Any:
        try:
            return _ops().tickets.resolve(ticket_id, payload)
        except InvalidStateTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/tickets/{ticket_id}/close")
    def close(ticket_id: str, payload: dict | None = None) -> Any:
        try:
            return _ops().tickets.close(ticket_id, payload)
        except InvalidStateTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/tickets/{ticket_id}/escalate")
    def escalate(ticket_id: str, payload: dict | None = None) -> Any:
        """Request approval for a high-risk action (AC-08).

        Ticket status is NOT changed: a PENDING approval leaves the
        ticket valid (invariant #6).
        """
        try:
            body = payload or {}
            return _ops().approvals.escalate(
                ticket_id,
                action=str(body.get("action") or "escalate"),
                requested_by=str(body.get("requested_by") or "operator"),
                reason=body.get("reason"),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/approvals")
    def list_approvals(status: str | None = None) -> Any:
        try:
            parsed = ApprovalStatus(status) if status else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid status: {status}") from exc
        return _ops().approvals.list(parsed)

    @app.post("/approvals/{approval_id}/approve")
    def approve(approval_id: str, payload: dict | None = None) -> Any:
        try:
            body = payload or {}
            return _ops().approvals.approve(
                approval_id, decided_by=str(body.get("decided_by") or "approver")
            )
        except InvalidApprovalDecision as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/approvals/{approval_id}/reject")
    def reject(approval_id: str, payload: dict | None = None) -> Any:
        try:
            body = payload or {}
            return _ops().approvals.reject(
                approval_id,
                decided_by=str(body.get("decided_by") or "approver"),
                reason=body.get("reason"),
            )
        except InvalidApprovalDecision as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


app = create_app()