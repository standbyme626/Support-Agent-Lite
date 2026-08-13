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
from app.application.memory_service import MemoryService
from app.application.retriever import Retriever
from app.application.session_service import SessionService
from app.application.support_agent import SupportAgent
from app.application.ticket_service import TicketResolver, TicketService
from app.application.workflow import SupportWorkflow
from app.domain.approval import ApprovalStatus, InvalidApprovalDecision
from app.domain.envelope import InboundEnvelope
from app.domain.identity import Session, User
from app.domain.memory import MemoryKind
from app.domain.ticket import InvalidStateTransition
from app.infrastructure.db import apply_migrations, connect
from app.infrastructure.idempotency import IdempotencyStore
from app.infrastructure.llm import LLMClient, llm_client_from_env
from app.infrastructure.repositories import (
    ApprovalRepository,
    ChannelIdentityRepository,
    MemoryRepository,
    MessageRepository,
    SessionRepository,
    TicketStore,
    UserRepository,
)
from app.infrastructure.trace import TraceLogger

_SEED_DIR = Path(__file__).resolve().parent.parent / "seed" / "faq"


@dataclass
class OpsServices:
    """Operator-facing services (tickets + independent approvals + memory)."""

    tickets: TicketService
    approvals: ApprovalService
    memory: MemoryService
    trace: TraceLogger


def build_memory(conn: Any, store: TicketStore) -> MemoryService:
    return MemoryService(store, MemoryRepository(conn))


def build_ops(conn: Any, store: TicketStore) -> OpsServices:
    """Assemble operator + approval + memory + trace services on one DB connection."""
    return OpsServices(
        tickets=TicketService(store),
        approvals=ApprovalService(store, ApprovalRepository(conn)),
        memory=build_memory(conn, store),
        trace=TraceLogger(conn),
    )


def _op_trace_id() -> str:
    """Fresh trace id for operator/approval actions (no inbound envelope)."""
    from app.domain.envelope import new_id

    return new_id("trace_")


def build_workflow(
    conn: Any,
    store: TicketStore,
    seed_dir: str | Path = _SEED_DIR,
    llm: LLMClient | None = None,
) -> SupportWorkflow:
    """Assemble the Phase 4/6 workflow (intent -> RAG or ticket -> agent + memory)."""
    return SupportWorkflow(
        router=IntentRouter(),
        retriever=Retriever(seed_dir),
        ticket_service=TicketService(store),
        resolver=TicketResolver(TicketService(store), store),
        context_builder=ContextBuilder(MessageRepository(conn)),
        agent=SupportAgent(llm=llm),
        messages=MessageRepository(conn),
        memory=build_memory(conn, store),
        trace=TraceLogger(conn),
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
        trace=TraceLogger(conn),
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
                "recalled": [m.fact for m in getattr(downstream, "recalled", [])],
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
        """Close a resolved ticket. CLOSED triggers memory extraction
        (AC-09): stable facts are stored for next-session recall."""
        try:
            result = _ops().tickets.close(ticket_id, payload)
            memories = _ops().memory.remember(ticket_id)
            _ops().trace.event(
                _op_trace_id(),
                "memory_extract",
                {"ticket_id": ticket_id, "facts": [m.fact for m in memories]},
            )
            return result
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
            approval = _ops().approvals.escalate(
                ticket_id,
                action=str(body.get("action") or "escalate"),
                requested_by=str(body.get("requested_by") or "operator"),
                reason=body.get("reason"),
            )
            _ops().trace.event(
                _op_trace_id(),
                "approval",
                {"approval_id": approval.id, "ticket_id": ticket_id, "action": approval.action, "status": approval.status.value},
            )
            return approval
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/approvals")
    def list_approvals(status: str | None = None) -> Any:
        try:
            parsed = ApprovalStatus(status) if status else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid status: {status}") from exc
        return _ops().approvals.list(parsed)

    @app.get("/memories")
    def list_memories(user_id: str | None = None, kind: str | None = None) -> Any:
        """Long-term memory (AC-09). Filter by canonical user and kind."""
        try:
            parsed_kind = MemoryKind(kind) if kind else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid kind: {kind}") from exc
        return _ops().memory.list(user_id, parsed_kind)

    @app.post("/approvals/{approval_id}/approve")
    def approve(approval_id: str, payload: dict | None = None) -> Any:
        try:
            body = payload or {}
            result = _ops().approvals.approve(
                approval_id, decided_by=str(body.get("decided_by") or "approver")
            )
            _ops().trace.event(
                _op_trace_id(),
                "approval",
                {"approval_id": approval_id, "status": result.status.value, "decided_by": result.decided_by},
            )
            return result
        except InvalidApprovalDecision as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/approvals/{approval_id}/reject")
    def reject(approval_id: str, payload: dict | None = None) -> Any:
        try:
            body = payload or {}
            result = _ops().approvals.reject(
                approval_id,
                decided_by=str(body.get("decided_by") or "approver"),
                reason=body.get("reason"),
            )
            _ops().trace.event(
                _op_trace_id(),
                "approval",
                {"approval_id": approval_id, "status": result.status.value, "decided_by": result.decided_by},
            )
            return result
        except InvalidApprovalDecision as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/traces/{trace_id}")
    def get_trace(trace_id: str) -> Any:
        """Inspect the full journey of one message: channel -> identity ->
        intent -> retrieval/ticket -> agent/memory -> reply."""
        events = _ops().trace.get(trace_id)
        if not events:
            raise HTTPException(status_code=404, detail=f"trace not found: {trace_id}")
        return {
            "trace_id": trace_id,
            "stages": [{"stage": e.stage, "payload": e.payload, "created_at": e.created_at} for e in events],
        }

    return app


app = create_app()