"""V2 shared message/assert helpers (fixtures live in conftest.py)."""
from __future__ import annotations

WECOM_REPAIR_GROUP = "repair_group_1"
WECOM_OPERATOR_GROUP = "op_group_facility"
WECOM_APPROVAL_ROOM = "approval_room"


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
