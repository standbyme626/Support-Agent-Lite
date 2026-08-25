"""FastAPI application bootstrap: channel webhooks + operator API + V2
collaboration (conversations, roles, actions, notifications, outbound)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


def _load_env_file() -> None:
    """Minimal .env loader (no external dependency).

    setdefault semantics: real environment variables always win over the
    file. Tests stay offline because conftest explicitly assigns
    REAL_CHANNEL_NETWORK=false before importing this module.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_env_file()

from app.adapters.base import ChannelAdapterError, HttpInbound
from app.adapters.feishu import FeishuAdapter
from app.adapters.outbound import FeishuConfig, FeishuOutboundClient, WeComConfig, WeComOutboundClient
from app.adapters.wecom import WeComAdapter
from app.application.approval_service import ApprovalService
from app.application.agent_tools import AgentToolPort
from app.application.command_parser import CommandParser
from app.application.context_builder import ContextBuilder
from app.application.conversation_service import ConversationService
from app.application.identity_service import IdentityResolver
from app.application.ingress_service import IngressService
from app.application.intent_router import IntentRouter
from app.application.memory_service import MemoryService
from app.application.notification_service import NotificationService
from app.application.policy import PolicyValidator
from app.application.retriever import Retriever
from app.application.role_service import RoleService
from app.application.session_service import SessionService
from app.application.support_agent import SupportAgent
from app.application.target_resolver import TargetResolver
from app.application.ticket_action_service import TicketActionService
from app.application.ticket_service import TicketResolver, TicketService
from app.application.workflow import SupportWorkflow
from app.domain.approval import ApprovalStatus, InvalidApprovalDecision
from app.domain.pending_action import ApprovableAction
from app.domain.envelope import InboundEnvelope
from app.domain.identity import Session, User
from app.domain.memory import MemoryKind
from app.domain.role import UserRole
from app.domain.ticket import AlreadyClaimed, InvalidStateTransition
from app.infrastructure.db import apply_migrations, connect
from app.infrastructure.idempotency import IdempotencyStore
from app.infrastructure.llm import LLMClient, llm_client_from_env
from app.infrastructure.repositories import (
    ApprovalRepository,
    ChannelIdentityRepository,
    ConversationRepository,
    MemoryRepository,
    MessageRepository,
    NotificationOutboxRepository,
    PendingActionRepository,
    RoleRepository,
    SessionRepository,
    SessionTicketContextRepository,
    TicketStore,
    UserRepository,
)
from app.infrastructure.trace import TraceLogger

_SEED_DIR = Path(__file__).resolve().parent.parent / "seed" / "faq"
_CONVERSATION_SEED_DIR = Path(__file__).resolve().parent.parent / "seed"


@dataclass
class OpsServices:
    """Operator-facing services (tickets + approvals + memory + actions)."""

    tickets: TicketService
    approvals: ApprovalService
    memory: MemoryService
    trace: TraceLogger
    actions: TicketActionService
    roles: RoleService
    conversations: ConversationService
    notifications: NotificationService
    identity: IdentityResolver
    users: UserRepository


def _channel_adapters() -> dict[str, object]:
    return {
        "wecom": WeComAdapter(
            token=os.environ.get("WECOM_TOKEN"),
            encoding_aes_key=os.environ.get("WECOM_ENCODING_AES_KEY"),
        ),
        "feishu": FeishuAdapter(
            verification_token=os.environ.get("FEISHU_VERIFICATION_TOKEN"),
            encrypt_key=os.environ.get("FEISHU_ENCRYPT_KEY"),
        ),
    }


def _outbound_clients(transport=None) -> dict[str, object]:
    from app.adapters.outbound import transport_from_env
    from app.adapters.transports import HttpTransport

    transport = transport or transport_from_env()
    return {
        "wecom": WeComOutboundClient(
            WeComConfig(
                corp_id=os.environ.get("WECOM_CORP_ID", ""),
                corp_secret=os.environ.get("WECOM_CORP_SECRET", ""),
                agent_id=os.environ.get("WECOM_AGENT_ID", ""),
            ),
            transport=transport,
        ),
        "feishu": FeishuOutboundClient(
            FeishuConfig(
                app_id=os.environ.get("FEISHU_APP_ID", ""),
                app_secret=os.environ.get("FEISHU_APP_SECRET", ""),
                verification_token=os.environ.get("FEISHU_VERIFICATION_TOKEN", ""),
            ),
            transport=transport,
        ),
    }


def build_memory(conn: Any, store: TicketStore) -> MemoryService:
    return MemoryService(store, MemoryRepository(conn))


def _build_directory(seed_dir: str | Path):
    from app.infrastructure.directory import DirectoryService

    base = Path(seed_dir)
    directory_dir = base.parent / "directory" if base.name == "faq" else base / "directory"
    return DirectoryService(directory_dir)


def _stats_agent(llm, store: TicketStore):
    from app.application.stats_agent import StatsAgent

    return StatsAgent(llm=llm, tickets=store)


def _ticket_index():
    from pathlib import Path as _Path

    from app.application.ticket_insights import TicketSimilarityIndex

    index_dir = _Path(__file__).resolve().parent.parent / "runtime" / "ticket_index"
    embedding = None
    try:
        import os

        if os.environ.get("KB_VECTOR_ENABLED", "").lower() in ("1", "true", "yes"):
            from app.infrastructure.vector_store import SiliconFlowEmbedding

            embedding = SiliconFlowEmbedding()
    except Exception:  # noqa: BLE001 - index is optional by design
        embedding = None
    return TicketSimilarityIndex(index_dir=index_dir, embedding=embedding)


def _maybe_hybrid(retriever: Retriever) -> Retriever:
    """Wrap the keyword retriever with vector+rerank fusion (C2).

    Activated by KB_VECTOR_ENABLED=true AND a built index on disk; any
    failure keeps the plain TF-IDF retriever (deterministic degradation).
    """
    import os

    if os.environ.get("KB_VECTOR_ENABLED", "").lower() not in ("1", "true", "yes"):
        return retriever
    try:
        from app.application.hybrid_retriever import HybridRetriever
        from app.infrastructure.vector_store import (
            NumpyVectorStore,
            SiliconFlowEmbedding,
            SiliconFlowReranker,
        )

        index_dir = Path(__file__).resolve().parent.parent / "runtime" / "vector_index"
        store = NumpyVectorStore(index_dir)
        if not store.load():
            return retriever
        return HybridRetriever(
            retriever,
            embedding=SiliconFlowEmbedding(),
            store=store,
            reranker=SiliconFlowReranker(),
        )
    except Exception as exc:  # noqa: BLE001 - retrieval must never hard-fail
        print(f"[c2] hybrid retriever unavailable, keyword-only: {exc!r}")
        return retriever


def build_core(conn: Any, store: TicketStore, outbound_clients: dict | None = None) -> dict[str, Any]:
    """Shared V2 services (roles/conversations/targets/outbox/actions)."""
    users = UserRepository(conn)
    conversations = ConversationService(ConversationRepository(conn), _CONVERSATION_SEED_DIR)
    roles = RoleService(RoleRepository(conn))
    targets = TargetResolver(ConversationRepository(conn), SessionRepository(conn), ChannelIdentityRepository(conn))
    outbox = NotificationOutboxRepository(conn)
    notifications = NotificationService(outbox, targets, outbound_clients or _outbound_clients())
    actions = TicketActionService(
        conn=conn,
        store=store,
        users=users,
        approvals=ApprovalService(store, ApprovalRepository(conn)),
        approval_repo=ApprovalRepository(conn),
        pending_actions=PendingActionRepository(conn),
        memory=build_memory(conn, store),
        notifications=notifications,
        targets=targets,
    )
    return {
        "conversations": conversations,
        "roles": roles,
        "notifications": notifications,
        "actions": actions,
    }


def build_ops(conn: Any, store: TicketStore, outbound_clients: dict | None = None) -> OpsServices:
    """Assemble operator + approval + memory + action + notification services."""
    core = build_core(conn, store, outbound_clients)
    users = UserRepository(conn)
    identities = ChannelIdentityRepository(conn)
    return OpsServices(
        tickets=TicketService(store),
        approvals=ApprovalService(store, ApprovalRepository(conn)),
        memory=build_memory(conn, store),
        trace=TraceLogger(conn),
        actions=core["actions"],
        roles=core["roles"],
        conversations=core["conversations"],
        notifications=core["notifications"],
        identity=IdentityResolver(users, identities),
        users=users,
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
    core: dict[str, Any] | None = None,
) -> SupportWorkflow:
    """Assemble the V2.1 workflow (purpose routing + bounded agent + HITL).

    The agent gets a read-only tool port and the policy validator sits
    between agent proposals and the approval pipeline.
    """
    core = core or build_core(conn, store)
    retriever = Retriever(seed_dir)
    retriever = _maybe_hybrid(retriever)
    memory = build_memory(conn, store)
    directory = _build_directory(seed_dir)
    stats_agent = _stats_agent(llm, store)
    ticket_index = _ticket_index()
    tools = AgentToolPort(store, MessageRepository(conn), retriever, memory,
                          directory=directory, stats_agent=stats_agent,
                          ticket_index=ticket_index)
    return SupportWorkflow(
        router=IntentRouter(),
        retriever=retriever,
        ticket_service=TicketService(store),
        resolver=TicketResolver(TicketService(store), store),
        context_builder=ContextBuilder(MessageRepository(conn)),
        agent=SupportAgent(llm=llm, tools=tools),
        messages=MessageRepository(conn),
        memory=memory,
        trace=TraceLogger(conn),
        conversations=core["conversations"],
        actions=core["actions"],
        roles=core["roles"],
        parser=CommandParser(),
        session_ctx=SessionTicketContextRepository(conn),
        policy=PolicyValidator(store),
    )


def build_ingress(
    db_path: str = ":memory:",
    downstream: Callable[[InboundEnvelope, User, Session, object], object] | None = None,
    seed_dir: str | Path = _SEED_DIR,
    llm: LLMClient | None = None,
    outbound_clients: dict | None = None,
) -> tuple[IngressService, Any, TicketStore]:
    """Assemble the two-phase ingress pipeline (V2.1).

    The V2.1 pipeline runs the workflow in prepare/run-agent/apply phases
    so the LLM never holds the database write lock. A legacy `downstream`
    callable is still accepted and runs in the single-transaction mode.
    """
    conn = connect(db_path)
    apply_migrations(conn)
    users = UserRepository(conn)
    identities = ChannelIdentityRepository(conn)
    sessions = SessionRepository(conn)
    store = TicketStore(conn)
    core = build_core(conn, store, outbound_clients)
    if downstream is not None:
        workflow = None
    else:
        workflow = build_workflow(conn, store, seed_dir=seed_dir, llm=llm, core=core)
    return IngressService(
        adapters=_channel_adapters(),  # type: ignore[arg-type]
        identity=IdentityResolver(users, identities),
        sessions=SessionService(sessions),
        idempotency=IdempotencyStore(conn),
        downstream=downstream,
        trace=TraceLogger(conn),
        conversations=core["conversations"],
        notifications=core["notifications"],
        workflow=workflow,
    ), conn, store


def create_app(ingress: IngressService | None = None, ops: OpsServices | None = None) -> FastAPI:
    """App factory; webhook and operator endpoints registered here.

    When no services are injected (module-level app), both are built
    lazily on first request with a default runtime DB path.
    """
    app = FastAPI(
        title="support-agent-lite",
        version="2.0.0",
        description=(
            "Cross-channel enterprise support agent with collaboration: "
            "conversation purposes, roles, notifications, outbound, HITL."
        ),
    )
    app.state.ingress = ingress
    app.state.ops = ops

    def _services() -> None:
        if app.state.ingress is None:
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

    def _dispatch() -> None:
        try:
            _ops().notifications.dispatch()
        except Exception:
            pass

    def _actor(body: dict, *, role: UserRole | None, endpoint: str) -> str:
        """REST trust boundary (V2.1): resolve + verify the canonical actor.

        The control-plane API no longer trusts arbitrary actor strings.
        Callers must name either a canonical `actor_user_id` or a channel
        identity `{"actor": {"channel": ..., "channel_user_id": ...}}`; the
        actor must EXIST as a canonical user and hold the required role.
        """
        ops = _ops()
        actor = body.get("actor")
        user_id: str | None = None
        if isinstance(actor, dict):
            channel = str(actor.get("channel") or "")
            channel_user_id = str(actor.get("channel_user_id") or "")
            if not channel or not channel_user_id:
                raise HTTPException(
                    status_code=400, detail="actor requires channel and channel_user_id"
                )
            user = ops.identity.find(channel, channel_user_id)
            user_id = user.id if user else None
        else:
            raw = body.get("actor_user_id")
            user_id = str(raw) if raw else None
        if not user_id:
            raise HTTPException(
                status_code=401,
                detail=f"{endpoint} requires an actor: actor_user_id or actor.channel/channel_user_id",
            )
        user = ops.users.get(user_id)
        if user is None:
            raise HTTPException(status_code=401, detail=f"unknown actor: {user_id}")
        if role is not None and not ops.roles.has_role(user_id, role):
            raise HTTPException(
                status_code=403, detail=f"actor lacks required role: {role.value}"
            )
        return user_id

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    def root() -> dict[str, str]:
        return {"service": "support-agent-lite", "docs": "/docs"}

    # --- channel webhooks (official protocol verification first) ---

    @app.api_route("/webhooks/{channel}", methods=["GET", "POST"])
    async def webhook(channel: str, request: Request) -> JSONResponse:
        try:
            adapter = _ingress()._adapters[channel]  # noqa: SLF001
            query = dict(request.query_params)
            raw_body = await request.body()
            inbound: HttpInbound = adapter.handle_http(request.method, query, raw_body)
            if inbound.error:
                raise ChannelAdapterError(channel, "verification", inbound.error)
            if inbound.challenge is not None:
                if isinstance(inbound.challenge, str):
                    return JSONResponse(content=inbound.challenge)
                return JSONResponse(content=inbound.challenge)
            payload = inbound.payload or (await request.json())
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
                "conversation": getattr(result.conversation, "purpose", None),
                "workflow": getattr(downstream, "kind", None),
                "ticket_id": getattr(downstream, "ticket", None) and downstream.ticket.id,
                "reply": getattr(downstream, "reply", None),
                "recalled": [m.fact for m in getattr(downstream, "recalled", [])],
            },
        )

    # --- Operator API ---

    @app.post("/tickets/{ticket_id}/claim")
    def claim(ticket_id: str, payload: dict | None = None) -> Any:
        try:
            body = payload or {}
            actor = _actor(body, role=UserRole.OPERATOR, endpoint="claim")
            outcome = _ops().actions.claim(
                ticket_id,
                actor,
                trace_id=_op_trace_id(),
            )
        except AlreadyClaimed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidStateTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _dispatch()
        return _ticket_response(outcome.ticket)

    @app.post("/tickets/{ticket_id}/resolve")
    def resolve(ticket_id: str, payload: dict | None = None) -> Any:
        try:
            body = payload or {}
            actor = _actor(body, role=UserRole.OPERATOR, endpoint="resolve")
            outcome = _ops().actions.resolve(
                ticket_id,
                actor,
                body.get("note"),
                trace_id=_op_trace_id(),
            )
        except InvalidStateTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _dispatch()
        return _ticket_response(outcome.ticket)

    @app.post("/tickets/{ticket_id}/close")
    def close(ticket_id: str, payload: dict | None = None) -> Any:
        """Legacy REST close (DEPRECATED): an unapproved direct close is no
        longer possible (V2.1 closure fix). This endpoint now requires a
        reason and routes through the FORCE_CLOSE approval pipeline; the
        ticket only closes when an approver approves."""
        try:
            body = payload or {}
            actor = _actor(body, role=UserRole.OPERATOR, endpoint="close")
            reason = body.get("reason")
            if not reason:
                raise HTTPException(
                    status_code=400,
                    detail="close requires a reason (unapproved direct close removed); "
                    "use /escalate-style FORCE_CLOSE approval flow",
                )
            outcome = _ops().actions.force_close(
                ticket_id,
                actor,
                str(reason),
                trace_id=_op_trace_id(),
            )
        except InvalidStateTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _dispatch()
        approval = _ops().approvals.get(outcome.approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return approval

    @app.post("/tickets/{ticket_id}/escalate")
    def escalate(ticket_id: str, payload: dict | None = None) -> Any:
        try:
            body = payload or {}
            actor = _actor(body, role=UserRole.OPERATOR, endpoint="escalate")
            requested_action = body.get("action")
            if requested_action and requested_action not in {a.value for a in ApprovableAction}:
                raise HTTPException(
                    status_code=400,
                    detail=f"action {requested_action} is not approvable; whitelist: {[a.value for a in ApprovableAction]}",
                )
            outcome = _ops().actions.escalate(
                ticket_id,
                actor,
                body.get("reason"),
                trace_id=_op_trace_id(),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _dispatch()
        approval = _ops().approvals.get(outcome.approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return approval

    @app.get("/approvals")
    def list_approvals(status: str | None = None) -> Any:
        try:
            parsed = ApprovalStatus(status) if status else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid status: {status}") from exc
        return _ops().approvals.list(parsed)

    @app.get("/memories")
    def list_memories(user_id: str | None = None, kind: str | None = None) -> Any:
        try:
            parsed_kind = MemoryKind(kind) if kind else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid kind: {kind}") from exc
        return _ops().memory.list(user_id, parsed_kind)

    @app.post("/approvals/{approval_id}/approve")
    def approve(approval_id: str, payload: dict | None = None) -> Any:
        try:
            body = payload or {}
            actor = _actor(body, role=UserRole.APPROVER, endpoint="approve")
            outcome = _ops().actions.approve(
                approval_id, actor, trace_id=_op_trace_id()
            )
        except InvalidApprovalDecision as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _dispatch()
        approval = _ops().approvals.get(approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return approval

    @app.post("/approvals/{approval_id}/reject")
    def reject(approval_id: str, payload: dict | None = None) -> Any:
        try:
            body = payload or {}
            actor = _actor(body, role=UserRole.APPROVER, endpoint="reject")
            _ops().actions.reject(
                approval_id,
                actor,
                body.get("reason"),
                trace_id=_op_trace_id(),
            )
        except InvalidApprovalDecision as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _dispatch()
        approval = _ops().approvals.get(approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return approval

    # --- V2 surfaces ---

    @app.post("/conversations/register")
    def register_conversation(payload: dict) -> Any:
        """Register a conversation purpose (config-like, persisted)."""
        conversation = _ops().conversations.register(
            channel=str(payload["channel"]),
            channel_conversation_id=str(payload["channel_conversation_id"]),
            conversation_type=str(payload["conversation_type"]),
            purpose=str(payload["purpose"]),
            queue=payload.get("queue"),
            location=payload.get("location"),
            enabled=bool(payload.get("enabled", True)),
        )
        return _conversation_dict(conversation)

    @app.get("/conversations")
    def list_conversations() -> Any:
        return [_conversation_dict(c) for c in _ops().conversations.list_all()]

    @app.get("/tickets/{ticket_id}/case")
    def get_case(ticket_id: str) -> Any:
        """Full case view (V2.1 closure): ticket + events (actors/traces) +
        notifications with delivery attempts + approvals + pending actions +
        memories. Approval/pending/delivery-attempt coverage is now part of
        the trace, matching the documented "full case trace" claim."""
        store = _ops().tickets
        ticket = store.get(ticket_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail=f"ticket not found: {ticket_id}")
        outbox = _ops().notifications._outbox  # noqa: SLF001
        approvals_repo = _ops().approvals._approvals  # noqa: SLF001
        pending_repo = _ops().actions._pending  # noqa: SLF001
        notifications = [
            {
                "type": n.notification_type.value,
                "visibility": n.visibility.value,
                "message": n.message,
                "status": n.status.value,
                "attempt_count": n.attempt_count,
                "delivery_attempts": outbox.attempts(n.id),
            }
            for n in _ops().notifications.list_for_ticket(ticket_id)
        ]
        return {
            "ticket": {
                "id": ticket.id,
                "status": ticket.status.value,
                "assignee_user_id": ticket.assignee_user_id,
                "queue": ticket.queue,
                "priority": ticket.priority,
                "source_conversation_id": ticket.source_conversation_id,
            },
            "events": [
                {
                    "event_type": e.event_type.value,
                    "actor_user_id": e.actor_user_id,
                    "trace_id": e.trace_id,
                    "payload": e.payload,
                }
                for e in _ops().tickets._store.events(ticket_id)  # noqa: SLF001
            ],
            "notifications": notifications,
            "approvals": [
                {
                    "id": a.id,
                    "action": a.action,
                    "status": a.status.value,
                    "requested_by": a.requested_by,
                    "reason": a.reason,
                    "decided_by": a.decided_by,
                    "decided_at": a.decided_at.isoformat() if a.decided_at else None,
                }
                for a in approvals_repo.list_by_ticket(ticket_id)
            ],
            "pending_actions": [
                {
                    "id": p.id,
                    "action_type": p.action_type.value,
                    "approval_id": p.approval_id,
                    "execution_status": p.execution_status.value,
                    "requested_by": p.requested_by,
                }
                for p in pending_repo.list_by_ticket(ticket_id)
            ],
            "memories": [m.fact for m in _ops().memory.list(user_id=None) if m.ticket_id == ticket_id],
        }

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


def _ticket_response(ticket: Any) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "status": ticket.status.value,
        "assignee_user_id": ticket.assignee_user_id,
        "summary": ticket.summary,
        "category": ticket.category,
        "priority": ticket.priority,
        "queue": ticket.queue,
    }


def _conversation_dict(conversation: Any) -> dict[str, Any]:
    return {
        "id": conversation.id,
        "channel": conversation.channel,
        "channel_conversation_id": conversation.channel_conversation_id,
        "conversation_type": conversation.conversation_type.value,
        "purpose": conversation.purpose.value,
        "queue": conversation.queue,
        "location": conversation.location,
        "enabled": conversation.enabled,
    }


app = create_app()
