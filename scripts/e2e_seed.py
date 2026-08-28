"""E2E 真人测试:隔离 DB 种子(不碰生产 runtime/support_agent.db)。

模拟生产身份拓扑:
- 张三(报修人):wecom/zhangsan <-> feishu/ou_zhangsan
- 李师傅(运维,OPERATOR facility):wecom/lihua <-> feishu/ou_lihua
- 王经理(APPROVER):wecom/manager
会话:
- wecom repair_group_1(REQUESTER 维修群)
- wecom op_group_facility(OPERATOR 处理群)
- wecom approval_room(APPROVAL 审批室)
- feishu oc_requester_group(REQUESTER 飞书报修群)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
from app.infrastructure.db import connect, apply_migrations

DB = Path(sys.argv[1] if len(sys.argv) > 1 else "runtime/e2e_live.db")


def main() -> None:
    db_path = str(DB)
    if Path(db_path).exists():
        Path(db_path).unlink()
    conn = connect(db_path)
    apply_migrations(conn)

    identity = IdentityResolver(UserRepository(conn), ChannelIdentityRepository(conn))
    roles = RoleService(RoleRepository(conn))
    conversations = ConversationService(ConversationRepository(conn))

    zhang = identity.resolve("wecom", "zhangsan", "张三")
    identity.bind("feishu", "ou_zhangsan", zhang.id)

    li = identity.resolve("wecom", "lihua", "李师傅")
    identity.bind("feishu", "ou_lihua", li.id)
    roles.ensure_role(li.id, UserRole.OPERATOR, queue="facility")

    manager = identity.resolve("wecom", "manager", "王经理")
    roles.ensure_role(manager.id, UserRole.APPROVER)

    convs = [
        ("wecom", "repair_group_1", ConversationType.GROUP, ConversationPurpose.REQUESTER, "facility"),
        ("wecom", "op_group_facility", ConversationType.GROUP, ConversationPurpose.OPERATOR, "facility"),
        ("wecom", "approval_room", ConversationType.GROUP, ConversationPurpose.APPROVAL, None),
        ("feishu", "oc_requester_group", ConversationType.GROUP, ConversationPurpose.REQUESTER, "facility"),
    ]
    for channel, cid, ctype, purpose, queue in convs:
        conversations.register(
            channel=channel,
            channel_conversation_id=cid,
            conversation_type=ctype,
            purpose=purpose,
            queue=queue,
        )
    conn.close()
    print(f"seeded {db_path}: 张三/李师傅/王经理 + 4 会话")


if __name__ == "__main__":
    main()