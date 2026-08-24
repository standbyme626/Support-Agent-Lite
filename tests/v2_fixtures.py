"""V2 shared message/assert helpers (fixtures live in conftest.py)."""
from __future__ import annotations

WECOM_REPAIR_GROUP = "repair_group_1"
WECOM_OPERATOR_GROUP = "op_group_facility"
WECOM_APPROVAL_ROOM = "approval_room"


def seed_control_plane(conn) -> dict:
    """Seed operator + approver canonical users for REST trust-boundary
    tests. Returns {name: user_id} (lihua = OPERATOR, manager = APPROVER)."""
    from app.application.identity_service import IdentityResolver
    from app.application.role_service import RoleService
    from app.domain.role import UserRole
    from app.infrastructure.repositories import (
        ChannelIdentityRepository,
        RoleRepository,
        UserRepository,
    )

    identity = IdentityResolver(UserRepository(conn), ChannelIdentityRepository(conn))
    roles = RoleService(RoleRepository(conn))
    li = identity.resolve("wecom", "lihua", "李师傅")
    identity.bind("feishu", "ou_lihua", li.id)
    roles.ensure_role(li.id, UserRole.OPERATOR, queue="facility")
    manager = identity.resolve("wecom", "manager", "王经理")
    identity.bind("feishu", "ou_manager", manager.id)
    roles.ensure_role(manager.id, UserRole.APPROVER)
    return {"lihua": li.id, "manager": manager.id}


OPERATOR_ACTOR = {"actor": {"channel": "wecom", "channel_user_id": "lihua"}}
APPROVER_ACTOR = {"actor": {"channel": "wecom", "channel_user_id": "manager"}}


# --- message helpers ---


def wecom_group(client, content: str, msg_id: str, conversation_id: str = WECOM_REPAIR_GROUP, user: str = "zhangsan"):
    return client.post(
        "/webhooks/wecom",
        json={
            "MsgId": msg_id,
            "FromUserName": user,
            "Content": content,
            "CreateTime": 1000,
            "conversation_id": conversation_id,
        },
    )


def wecom_dm(client, content: str, msg_id: str, user: str = "zhangsan"):
    """No conversation_id -> adapter defaults to the channel user id (DM)."""
    return client.post(
        "/webhooks/wecom",
        json={"MsgId": msg_id, "FromUserName": user, "Content": content, "CreateTime": 1000},
    )


def feishu_official(client, text: str, event_id: str, open_id: str, chat_type: str = "p2p", chat_id: str | None = None, message_id: str | None = None):
    """Official im.message.receive_v1 shaped payload (see CHANNEL_PROTOCOL_MATRIX)."""
    return client.post(
        "/webhooks/feishu",
        json={
            "schema": "2.0",
            "header": {
                "event_id": event_id,
                "event_type": "im.message.receive_v1",
                "create_time": "1608725989000",
                "token": "test-token",
                "app_id": "cli_test",
                "tenant_key": "tk_test",
            },
            "event": {
                "sender": {
                    "sender_id": {"open_id": open_id, "user_id": "u1", "union_id": "on_1"},
                    "sender_type": "user",
                    "tenant_key": "tk_test",
                },
                "message": {
                    "message_id": message_id or f"om_{event_id}",
                    "create_time": "1609073151345",
                    "chat_id": chat_id or ("" if chat_type == "p2p" else "oc_group"),
                    "chat_type": chat_type,
                    "message_type": "text",
                    "content": f'{{"text":"{text}"}}',
                },
            },
        },
    )


def outbox_for(ctx, ticket_id: str) -> list[dict]:
    return ctx.client.get(f"/tickets/{ticket_id}/case").json()["notifications"]
