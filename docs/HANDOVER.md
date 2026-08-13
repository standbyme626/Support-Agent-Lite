# 工作交接 — Support-Agent-Lite

> 给下一个会话的完整交接。请先读本文件,再读 AGENTS.md 与相关 docs。

---

## 1. 项目一句话

`support-agent-lite` 是一个**跨渠道(企业微信/飞书)企业内部支持代理**,以**用户为中心(User-Centric)、工作流优先(Workflow-First)**重新实现,目标面试可展示、真实跑通 Golden Path。

## 2. 仓库与远程

| 项 | 值 |
| --- | --- |
| 本地路径 | `/home/kkk/Project/support-agent-platform` |
| 远程仓库 | `git@github.com:standbyme626/Support-Agent-Lite.git` |
| SSH 认证 | 已配置:`~/.ssh/id_ed25519`(已添加到 GitHub,用户名 `standbyme626`) |
| git 身份 | 已配置全局 `root <root@PC.localdomain>` |
| 当前分支 | `main`,工作区干净 |

## 3. 已完成进度(4 个提交,46 测试全绿)

| Phase | 提交 | 内容 |
| --- | --- | --- |
| 0 | `c029788` | 项目骨架:docs/AGENTS.md/pyproject/FastAPI+`/health`/pytest/Dockerfile |
| 1 | `6018685` | 领域实体(User/ChannelIdentity/Session/Ticket/TicketEvent)、严格状态机(OPEN→IN_PROGRESS→RESOLVED→CLOSED)、迁移 0001-0003、事务性 Ticket+Event |
| 2 | `34f197e` | IdentityResolver(渠道身份→规范用户,含 `bind` 跨渠道绑定)、SessionService、TicketService(T0001 递增单号)、TicketResolver(显式单号→会话单号→唯一活跃单→新建/澄清,纯规则无 LLM 随机) |
| 3 | `508f8a2` | WeCom/Feishu 适配器(Raw→InboundEnvelope)、`POST /webhooks/{channel}`、IdempotencyStore(migration 0004, AC-03) |

## 4. 关键文件地图

```
app/
├── main.py                    # FastAPI 工厂 + /health + /webhooks/{channel}
├── domain/
│   ├── envelope.py            # InboundEnvelope(channel/message_id/channel_user_id/conversation_id/text/timestamp/trace_id)
│   ├── identity.py            # User / ChannelIdentity / Session
│   └── ticket.py              # Ticket / TicketEvent / TicketStatus / 状态机校验
├── application/
│   ├── identity_service.py    # IdentityResolver(resolve/bind)
│   ├── session_service.py     # SessionService(find_or_create)
│   ├── ticket_service.py      # TicketService + TicketResolver + new_ticket_id
│   └── ingress_service.py     # IngressService(process, 幂等入口)
├── adapters/
│   ├── base.py                # ChannelAdapter 协议 + ChannelAdapterError
│   ├── wecom.py               # WeComAdapter(MsgId/FromUserName/Content)
│   └── feishu.py              # FeishuAdapter(message_id/open_id)
└── infrastructure/
    ├── db.py                  # connect(check_same_thread=False) + apply_migrations
    ├── idempotency.py         # IdempotencyStore
    └── repositories.py        # User/ChannelIdentity/Session/TicketStore
storage/migrations/0001-0004   # users, sessions, tickets+ticket_events, processed_messages
tests/                         # 46 个测试(Phase0/1/2/3)
docs/                          # 产品/架构/领域/黄金路径/验收/开发计划/移植对照
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

## 6. 运行与验证

```bash
source .venv/bin/activate        # 虚拟环境已存在
pytest                           # 46 passed
uvicorn app.main:app --port 8000 # 启动服务
curl http://127.0.0.1:8000/health
```

LLM 配置(OpenRouter,密钥在 `.env` 已被 gitignore):
`LLM_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free`,`LLM_BASE_URL=https://openrouter.ai/api/v1`,key 在 `.env` 的 `LLM_API_KEY`。

## 7. 下一步:Phase 4 — RAG + Agent + Summary

按 `docs/DEVELOPMENT_PLAN.md`,实现:

1. **IntentRouter**:意图集合 `faq / support / progress_query / other`(规则优先,可选 LLM fallback;参考旧代码 `reference/core/intent_router.py` 的 ADAPT)
2. **Retriever**:FAQ 文档检索 + source attribution;低置信必须有 no-answer 保护(不变量 #7),不得自由发挥
3. **ContextBuilder**:组装 ticket summary + recent messages
4. **SupportAgent**:只输出 summary / category / priority_suggestion / recommended_action / reply draft
   **禁止**直接 close/escalate/改状态(不变量 #4)
5. **Workflow**:FAQ→RAG→grounded answer(不建单);Support→TicketResolver→Ticket(用 Phase 2 已有的)

**验收**(docs/ACCEPTANCE_TESTS.md):
- AC-01 企业微信 FAQ:意图 FAQ→RAG→有出处答案→不建单
- AC-02 企业微信自动建单:`A3 空调坏了`→T 单→OPEN→created 事件
- AC-07 Agent summary 正确构建上下文
- FAQ evaluation Recall@3 ≥ 90%(目标)

## 8. 注意事项

- **密钥安全**:`.env` 含真实 OpenRouter key,已被 gitignore,绝不要提交/推送
- **reference/ 只读**:旧项目是参考物,禁止修改,也不要提交(已 gitignore)
- **不变量 #4**:Agent 只出建议,状态变更必须走 TicketService
- **不变量 #7**:RAG 低置信必须显式保护,不许 LLM 自由发挥
- **工作流**:每个任务先读相关 docs → 看现有接口 → 先写测试 → 最小改动 → 跑全量测试 → 提交推送
- 提交前跑 `pytest` 全量;推送用 SSH(remote 已是 `git@github.com:...`)

## 9. 用户偏好记录

- README 必须中文
- 开发节奏:一个 Phase 一个提交,每步验证后推送
- 旧代码策略:P/ADAPT/REWRITE/IGNORE,禁止整模块 PORT 未批准项(见 docs/LEGACY_PORT_MAP.md)
- 用户当前关注:后续会做 Phase 4(用真实 LLM),以及面试展示导向的 Demo
