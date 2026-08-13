# 工作交接 — Support-Agent-Lite

> 给下一个会话的完整交接。请先读本文件,再读 AGENTS.md 与相关 docs。

---

## 1. 项目一句话

`support-agent-lite` 是一个**跨渠道(企业微信/飞书)企业内部支持代理**,以**用户为中心(User-Centric)、工作流优先(Workflow-First)**实现。V1 完成 Core(AI Support Core,AC-01~10),**V2 完成 Full Collaboration Layer**(AC-11~30):Conversation Purpose / Role / Canonical Operator / 三面可见性 / 事务性通知 outbox / 官方协议契约(Mock network, not protocol)/ 确认关闭 / HITL 执行链 / 并发修复。

## 2. 仓库与远程

| 项 | 值 |
| --- | --- |
| 本地路径 | `/home/kkk/Project/support-agent-platform` |
| 远程仓库 | `git@github.com:standbyme626/Support-Agent-Lite.git` |
| SSH 认证 | 已配置:`~/.ssh/id_ed25519`(已添加到 GitHub,用户名 `standbyme626`) |
| git 身份 | 已配置全局 `root <root@PC.localdomain>` |
| 当前分支 | `main` |

## 3. 已完成进度

| Phase | 提交 | 内容 |
| --- | --- | --- |
| 0-7 (V1) | `c029788` → `8627b0f` | 骨架/领域/身份/渠道/RAG/协作/记忆/Trace,AC-01~10 全绿,139 测试基线 |
| V2 | 待提交 | 完整协作层(见 `docs/V2_IMPLEMENTATION_REPORT.md`),当前 **178 passed**,41 个 V2 测试(协作 12 · 并发 4 · HITL/通知 6 · 协议契约 14 · 离线 Demo 5) |

V2 关键变更:

- **迁移 0009-0012**:conversations+user_roles / tickets 运营字段(assignee/summary/category/priority/queue/source_conversation_id)+ticket_events 审计列(actor_user_id/trace_id/conversation_id)/ pending_actions / notification_outbox(+delivery_attempts)+session_ticket_contexts
- **领域新增**:`app/domain/{conversation,role,notification,outbound,pending_action}.py`;ticket 事件改为 `claimed/resolution_rejected/escalated/force_closed`,新合法迁移 `IN_PROGRESS→CLOSED`、`RESOLVED→IN_PROGRESS`
- **服务新增**:conversation_service / role_service / command_parser / target_resolver / notification_service / ticket_action_service;workflow 重写为 purpose 路由
- **协议**:Feishu token/challenge/AES;WeCom sha1 签名/AES/XML;出站契约(Feishu `im/v1/messages`,WeCom `message/send`+`appchat/send`);**WeCom GROUP_INBOUND 因官方文本消息无 chat_id 标 `PENDING_OFFICIAL_SPEC`**
- **并发修复**:幂等 claim 与业务同事务、原子 claim(WHERE OPEN+assignee IS NULL)、身份 IntegrityError 重读、SerializedConnection(连接级 RLock)、嵌套安全 `txn()`

## 4. 关键文件地图

```
app/
├── main.py                    # FastAPI 装配 + /webhooks/{channel} + /conversations + /tickets/{id}/actions|approval|case
├── domain/
│   ├── envelope.py            # InboundEnvelope
│   ├── identity.py            # User / ChannelIdentity / Session
│   ├── ticket.py              # Ticket / TicketEvent / 状态机 / AlreadyClaimed
│   ├── conversation.py        # Conversation / ConversationType / ConversationPurpose
│   ├── role.py                # Role(requester/operator/approver + queue)
│   ├── notification.py        # NotificationType / Visibility / OutboxRecord
│   ├── outbound.py            # ChannelCapability / DeliveryTarget / OutboundMessage
│   └── pending_action.py      # HITL 待执行动作
├── application/
│   ├── identity_service.py    # IdentityResolver(并发 IntegrityError 重读)
│   ├── conversation_service.py# ConversationService(register/list_all/operator_conversation)
│   ├── role_service.py        # RoleService(权限判定)
│   ├── command_parser.py      # slash 命令 + 中文别名 + 用户确认/驳回检测
│   ├── target_resolver.py     # audience/visibility → 具体目标
│   ├── notification_service.py# outbox 同事务入队 + dispatch 后置(重试≤3)
│   ├── ticket_action_service.py# 确定性动作执行器 + HITL _execute
│   ├── ingress_service.py     # 原子幂等 + purpose 路由 + dispatch
│   ├── workflow.py            # REQUESTER/OPERATOR/APPROVAL 路由 + NO_ANSWER 真实转人工
│   └── ...(V1 既有服务)
├── adapters/
│   ├── base.py                # ChannelAdapter 协议 + VerificationError + InboundRequest
│   ├── feishu.py              # 官方形状解析 + challenge + token + AES-256-CBC
│   ├── wecom.py               # sha1 签名 + AES 解密 + XML + 能力诚实声明
│   ├── transports.py          # HttpTransport(记录/fail_next)+ RealHttpTransport
│   └── outbound.py            # Feishu/WeCom 官方出站请求构造
└── infrastructure/
    ├── db.py                  # SerializedConnection(RLock)+ apply_migrations
    ├── idempotency.py         # 原子幂等 claim(同事务)
    └── repositories.py        # txn() 嵌套安全 + 全部仓储
storage/migrations/0001-0012
seed/conversations.json        # 演示群注册(repair_group_1/op_group_facility/approval_room/feishu oc_op_facility)
tests/                         # V1 + v2 5 个文件(conftest/v2_fixtures)
docs/CHANNEL_PROTOCOL_MATRIX.md   # 协议证据矩阵(官方 URL + 验证日期 2026-08-13)
docs/V2_IMPLEMENTATION_REPORT.md  # V2 实现报告(指标/遗留/未来接入)
V1_TO_V2_ARCHITECTURE_AUDIT.md    # V2 前置只读审计
reference/                     # 旧项目只读快照(gitignored,禁止修改)
```

## 5. 核心不变量(AGENTS.md)

1. Channel identity ≠ canonical user
2. Session ≠ user
3. Session ≠ memory
4. Agent 不得直接改敏感 Ticket 状态
5. Ticket 状态与 TicketEvent 必须同事务提交
6. Approval 是独立状态机
7. 低置信 RAG 不得变成自由发挥的模型答案
8. 跨渠道续单必须经规范用户身份解析
9. Channel != Role(渠道能力 ≠ 业务规则)
10. 共享 Operator 群无隐式 active ticket(动作必须显式带工单号)
11. Mock network, not protocol(官方文档唯一协议来源;无法证明标 `UNSUPPORTED/PENDING_OFFICIAL_SPEC`)

## 6. 运行与验证

```bash
source .venv/bin/activate
pytest                           # 178 passed
uvicorn app.main:app --port 8000
```

真实 LLM 可选(OpenRouter,`.env` 的 `LLM_API_KEY`):摘要/回复草稿润色,超时自动降级为确定性规则。**不要把 `.env` 提交。**

## 7. 协议接入(未来)

给真实渠道配置(App ID/Secret/Corp ID/Agent ID/Token/EncodingAESKey/Verification Token/Encrypt Key + 真实 conversation ids)后:

- 设 `REAL_CHANNEL_NETWORK=true` → 换 `RealHttpTransport`
- 配 webhook 回调 URL
- **无需重设计** Identity/Conversation/Ticket/Notification/Workflow

## 8. 注意事项

- **密钥安全**:`.env` 含真实 OpenRouter key,已被 gitignore,绝不要提交/推送
- **reference/ 只读**:旧项目是参考物,禁止修改,也不要提交(已 gitignore)
- **协议**:只依据官方文档(`docs/CHANNEL_PROTOCOL_MATRIX.md` 已记录);Legacy HMAC/桥接行为不是协议来源;WeCom GROUP_INBOUND 保持 `PENDING_OFFICIAL_SPEC`
- **不变量**:Agent 只出建议;Ticket+Event+Outbox 同事务;并发测试不能失效
- **工作流**:每个任务先读相关 docs → 看现有接口 → 先写测试 → 最小改动 → 跑全量测试 → 提交推送
- 提交前跑 `pytest` 全量;推送用 SSH(remote 已是 `git@github.com:...`)

## 9. 用户偏好记录

- README 必须中文
- 开发节奏:一个 Phase 一个提交,每步验证后推送
- 旧代码策略:PORT/ADAPT/REWRITE/IGNORE,禁止整模块 PORT 未批准项(见 docs/LEGACY_PORT_MAP.md)
- 用户当前关注:V2 收尾(文档已写、待提交推送);面试展示导向的 Demo(`tests/test_demo_v2.py` 16 步)
