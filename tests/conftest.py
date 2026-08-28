"""Pytest fixtures for V2 acceptance tests.

Hermeticity (V2.1, AC-A18): the default test run must NEVER touch the
real network, even when the shell/.env exports REAL_CHANNEL_NETWORK=true.
This is enforced UNCONDITIONALLY here (before any app import can load
.env): production credentials leaking into a test run must be impossible.
A test that genuinely needs the real network opts in by setting
os.environ["REAL_CHANNEL_NETWORK"]="true" inside its own body (and is
responsible for cleaning up after itself).
"""
from __future__ import annotations

import os

os.environ["REAL_CHANNEL_NETWORK"] = "false"
# Hermeticity: vector/rerank backends are external services — tests always
# run keyword-only unless a test explicitly opts in.
os.environ["KB_VECTOR_ENABLED"] = "false"
# Hermeticity: production defaults to INTENT_EMBEDDING=setfit (.env), which
# would load a ~2min CPU model into every test fixture. Tests stay on the
# API-anchor semantic layer unless a test explicitly opts in.
os.environ["INTENT_EMBEDDING"] = "api"

import pytest
from fastapi.testclient import TestClient

from app.adapters.transports import HttpTransport
from app.application.conversation_service import ConversationService
from app.application.identity_service import IdentityResolver
from app.application.role_service import RoleService
from app.domain.conversation import ConversationPurpose, ConversationType
from app.domain.role import UserRole
from app.infrastructure.repositories import (
    ChannelIdentityRepository,
    ConversationRepository,
    RoleRepository,
    UserRepository,
)
from app.main import build_ingress, build_ops, create_app

WECOM_REPAIR_GROUP = "repair_group_1"
WECOM_OPERATOR_GROUP = "op_group_facility"
WECOM_APPROVAL_ROOM = "approval_room"


class AppCtx:
    def __init__(self, client, conn, store, transport, users, ingress=None) -> None:
        self.client = client
        self.conn = conn
        self.store = store
        self.transport = transport
        self.users = users  # {name: user_id}
        self.ingress = ingress  # IngressService (for agent/LLM injection in tests)

    def with_llm(self, llm) -> None:
        """Swap the workflow agent's LLM (deterministic fake LLMs only)."""
        self.ingress._workflow._agent._llm = llm  # noqa: SLF001


def _outbound_clients(transport: HttpTransport) -> dict:
    from app.adapters.outbound import FeishuOutboundClient, WeComOutboundClient

    return {
        "wecom": WeComOutboundClient(transport=transport),
        "feishu": FeishuOutboundClient(transport=transport),
    }


@pytest.fixture()
def app_ctx() -> AppCtx:
    transport = HttpTransport()
    clients = _outbound_clients(transport)
    ingress, conn, store = build_ingress(db_path=":memory:", outbound_clients=clients)
    ops = build_ops(conn, store, clients)
    client = TestClient(create_app(ingress, ops))

    conversations = ConversationService(ConversationRepository(conn))
    conversations.register(
        channel="wecom",
        channel_conversation_id=WECOM_REPAIR_GROUP,
        conversation_type=ConversationType.GROUP,
        purpose=ConversationPurpose.REQUESTER,
        queue="facility",
    )
    identity = IdentityResolver(UserRepository(conn), ChannelIdentityRepository(conn))
    roles = RoleService(RoleRepository(conn))

    zhang = identity.resolve("wecom", "zhangsan", "张三")
    identity.bind("feishu", "ou_zhangsan", zhang.id)

    li = identity.resolve("wecom", "lihua", "李师傅")
    identity.bind("feishu", "ou_lihua", li.id)
    roles.ensure_role(li.id, UserRole.OPERATOR, queue="facility")

    manager = identity.resolve("wecom", "manager", "王经理")
    identity.bind("feishu", "ou_manager", manager.id)
    roles.ensure_role(manager.id, UserRole.APPROVER)

    users = {
        "zhangsan": zhang.id,
        "lihua": li.id,
        "manager": manager.id,
    }
    yield AppCtx(client, conn, store, transport, users, ingress)
    conn.close()
