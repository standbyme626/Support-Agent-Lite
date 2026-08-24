# Support Agent Lite

> 跨渠道企业支持代理(Cross-Channel Enterprise Support Agent)——**用户为中心(User-Centric)、工作流优先(Workflow-First)**。

企业微信 / 飞书消息 → 规范用户身份 → Conversation Purpose 路由 → FAQ 直答(不建单)或自动建单 → **有状态 SupportAgent 语义理解(多轮续单 / 紧急度识别 / 记忆召回 / RAG 证据问答 / 澄清追问 / 动作提案)** → 三面协作(用户群回执 / 私聊详情 / 运维工单)→ 人工认领 → 解决 → **用户确认关闭** → 记忆抽取 → 新会话召回。完整闭环,本地可跑、离线可演示、面试可展示。

- **V1(Core)** AC-01 ~ AC-10:Canonical Identity、跨渠道工单解析、四状态单状态机、RAG grounding、Agent advice-only、Long-term Memory、全链路 Trace
- **V2(Collaboration Layer)** AC-11 ~ AC-30:Conversation Purpose / Role / Canonical Operator / 三面可见性 / 事务性通知 Outbox / 官方协议契约 / 确认关闭 / HITL 执行链 / 并发修复
- **V2.1(Agent Core)** AC-A01 ~ AC-A20:有状态有边界的 SupportAgent(AgentContext 全感知 → 有限只读工具 → 结构化 AgentDecision)、两阶段事务(LLM 不占写锁)、崩溃可恢复处理状态、PromptRegistry、Agent eval 套件、PRIVATE_DETAIL 首联直投、关闭后门收口、REST actor/role 信任边界

> 旧项目 `support-agent-platform` 以只读方式保存在 `reference/`,仅作参考,禁止修改/提交。

---

## 目录

- [总体架构](#总体架构)
- [核心不变量](#核心不变量)
- [V2.1 Agent Core](#v21-agent-core)
- [消息处理流程图](#消息处理流程图)
- [工单状态机](#工单状态机)
- [协作与通知链路](#协作与通知链路)
- [技术栈](#技术栈)
- [目录结构](#目录结构)
- [领域模型](#领域模型)
- [渠道协议(Mock network, not protocol)](#渠道协议)
- [API 一览](#api-一览)
- [快速开始](#快速开始)
- [一键演示](#一键演示)
- [测试与质量指标](#测试与质量指标)
- [真实渠道接入(未来)](#真实渠道接入未来)
- [文档](#文档)

---

## 总体架构

### 分层视图

```text
┌─────────────────────────────────────────────────────────────────────┐
│ transport / HTTP                                                     │
│   GET|POST /webhooks/{channel}   渠道入口(验签 + challenge + 消息)      │
│   POST /conversations/register   会话注册(purpose/queue/location)     │
│   POST /tickets/{id}/actions     认领 / 解决 / 强制关闭 / 升级         │
│   POST /tickets/{id}/approval    审批(approve / reject)              │
│   GET  /tickets/{id}/case        全案追踪(事件/通知/记忆/actor)         │
│   GET  /memories · /traces · /approvals · /health                    │
├─────────────────────────────────────────────────────────────────────┤
│ application(服务层)                                                    │
│   IdentityResolver · ConversationService · RoleService               │
│   TicketService · TicketActionService(确定性执行器)                   │
│   NotificationService(事务性 Outbox) · TargetResolver                │
│   CommandParser · Workflow(purpose 路由) · SupportAgent(有状态 Agent) │
│   AgentToolPort(只读) · PolicyValidator(提案门禁) · PromptRegistry    │
│   MemoryService · ApprovalService · TraceLogger · IngressService(两阶段) │
├─────────────────────────────────────────────────────────────────────┤
│ domain(领域模型)                                                       │
│   User / ChannelIdentity / Session · Conversation(Type/Purpose)     │
│   Role(requester|operator|approver) · Ticket(单状态) / TicketEvent   │
│   Approval(独立状态机) · PendingAction(HITL) · Notification(Outbox)   │
│   Outbound(Capability/Target/Message) · Memory · Message · Trace     │
├─────────────────────────────────────────────────────────────────────┤
│ infrastructure                                                        │
│   repositories(事务性仓储 + txn() 嵌套安全) · SQLite(串行化连接)         │
│   IdempotencyStore(原子幂等) · Retriever(FAQ 索引)                     │
│   LlmClient(可选,超时自动降级) · TraceStore · InboundProcessingStore   │
│   (RECEIVED→AGENT_PENDING→AGENT_COMPLETED→COMPLETED/FAILED_RETRYABLE) │
├─────────────────────────────────────────────────────────────────────┤
│ adapters(协议适配,严禁触碰业务)                                           │
│   FeishuAdapter(官方 im.message.receive_v1 / token / challenge / AES) │
│   WeComAdapter(官方 sha1 签名 / AES 解密 / XML / echostr)              │
│   OutboundClient(Feishu im/v1/messages · WeCom message/send+appchat) │
│   HttpTransport(记录型,离线) · RealHttpTransport(未来真实联网)           │
└─────────────────────────────────────────────────────────────────────┘
```

### 核心职责链(一张图看懂 V2.1)

```mermaid
flowchart LR
    subgraph Channel["渠道层(官方协议)"]
        W["企业微信 / 飞书<br/>webhook"]
        A["Adapter<br/>验签/解密/官方形状解析"]
    end
    subgraph Core["核心层(V1 已验证)"]
        ID["Canonical Identity<br/>渠道身份→规范用户"]
        CV["Conversation<br/>Type + Purpose"]
        IR["Intent Router<br/>FAQ | Support | Progress"]
    end
    subgraph AgentCore["Agent 层(V2.1)"]
        CTX["AgentContext<br/>消息/角色/会话/工单/记忆/RAG"]
        AGT["SupportAgent<br/>有状态·有边界·结构化决策"]
        TOOL["只读工具(≤2 次)<br/>历史/知识/记忆/允许动作"]
    end
    subgraph Collab["协作层(V2)"]
        TS["Ticket Resolver<br/>显式单号→续单/建单"]
        WF["Workflow<br/>REQUESTER/OPERATOR/APPROVAL"]
        POL["PolicyValidator<br/>提案→审批门禁"]
        ACT["TicketActionService<br/>确定性动作执行"]
        NOT["Notification<br/>Outbox + Target Resolver"]
        HITL["HITL<br/>审批→执行一次"]
    end
    subgraph Memory["记忆层"]
        CLOSE["CLOSED"]
        MEM["MemoryExtractor<br/>长期记忆"]
    end

    W --> A --> ID --> CV --> IR
    IR -->|FAQ| RAG["RAG 检索<br/>grounded 证据"]
    IR -->|Support| TS
    TS --> WF
    WF -->|建单| ACT
    WF -->|Agent 决策| CTX
    CTX --> AGT
    AGT -->|信息不足| TOOL
    TOOL -->|只读结果| AGT
    AGT -->|AgentDecision| WF
    WF -->|proposal| POL
    POL -->|审批| HITL
    HITL -->|APPROVED→执行| ACT
    ACT -->|业务事件| NOT
    NOT -->|出站| A
    ACT -->|RESOLVED| CLOSE
    CLOSE --> MEM
    MEM -->|新会话召回| CTX
```

---

## 核心不变量

1. **渠道身份 ≠ 规范用户**(`wecom/zhangsan` 与 `feishu/ou_001` → 同一个 `user_001`)
2. **Session ≠ 用户**(会话属于用户,但不是身份)
3. **Session ≠ 记忆**(记忆由规范用户承载,与渠道/会话无关)
4. **Agent 只出建议**(`AgentProposes. Policy validates. Domain decides.`):Agent 理解、总结、推荐并输出结构化 `AgentDecision`,但 claim/resolve/close/escalate/approve/assign/update 必须走确定性 Policy → Domain Service → HITL;Agent 无任何写工具,`action_proposal` 无业务效果
5. **Ticket 状态与 TicketEvent 同事务**(外加 V2:Notification Outbox 记录也同事务)
6. **Approval 是独立状态机**(PENDING 不冻结工单)
7. **低置信 RAG 不得自由发挥**(无可靠资料 → 真实转人工或诚实告知)
8. **跨渠道续单必须经规范用户身份解析**
9. **Channel ≠ Role**(同一渠道可有 Requester/Operator/Approver 会话;群/单聊能力是 Channel Capability,不是业务规则)
10. **共享 Operator 群无隐式 active ticket**(动作必须显式带工单号 `/claim T0001`)
11. **Mock network, not protocol**(协议只依据官方文档;无法证明的能力标 `UNSUPPORTED / PENDING_OFFICIAL_SPEC`)

---

## V2.1 Agent Core

> SupportAgent 是一个**有状态、有边界、可解释的企业支持 Agent**:消费完整对话/工单/记忆/知识上下文,可调用少量只读工具,输出 schema 校验过的 `AgentDecision`。敏感业务状态变更仍完全由确定性 Policy / Domain Service / HITL 控制。**不是 autonomous agent,没有无限 ReAct loop。**

### 运行模型

```text
Inbound Message
      ↓
Deterministic Ingress(身份 / 会话 / 幂等 / processing state)
      ↓
Deterministic Pre-routing(IntentRouter · TicketResolver · 显式命令)
      ↓
AgentContext(全感知:消息/角色/会话用途/工单/最近对话/记忆/RAG 证据)
      ↓
Bounded SupportAgent(max 3 steps · max 2 只读工具)
      ↓
Structured AgentDecision(validate_decision 兜底全部失败模式)
      ↓
PolicyValidator(角色/工单状态/允许动作/原因/风险/审批要求)
      ↓
HITL(需审批 → PendingAction → Approval)
      ↓
TicketActionService(确定性执行 → TicketEvent → Notification/Outbox)
```

### AgentDecision 契约(核心字段)

| 字段 | 含义 |
| --- | --- |
| `understanding` / `summary` | 语义理解 / 持久化摘要 |
| `category` · `priority_suggestion` | 分类 / 优先级建议(枚举校验) |
| `recommended_action` | `dispatch_repair \| network_triage \| software_support \| credential_reset \| finance_review \| hr_review \| assign_operator \| ask_clarification \| faq_answer` |
| `missing_information[]` | 信息不足 → 真实澄清能力 |
| `confidence`(0~1,越界钳制) | 决策置信度 |
| `memory_refs[]` / `knowledge_refs[]` | 只允许引用上下文真实存在的 id(反幻觉) |
| `action_proposal` | `ESCALATE \| FORCE_CLOSE`,**无业务效果**,必须过 Policy + Approval |
| `rationale` | 给系统/审计的简短可解释理由,**绝不保存 chain-of-thought** |
| `reply_draft` | ≤300 字的回复草稿(超长即 fallback) |

### 只读工具(第一版全部 READ ONLY)

| 工具 | 用途 | 限制 |
| --- | --- | --- |
| `get_ticket_history(ticket_id)` | 工单完整事件历史 + 最近会话 | ≤2 次/run |
| `search_knowledge(query)` | 检索企业知识库 | ≤2 次/run |
| `recall_memory(query)` | 该员工既往工单记忆 | ≤2 次/run |
| `get_allowed_actions(ticket_id, actor_role)` | 当前状态允许的动作 | ≤2 次/run |

**没有** claim/resolve/close/approve/reject/assign/update/execute_action——写工具不存在。

### 两阶段事务模型(LLM 不占 DB 写锁)

```text
事务 A(入站事务)
  claim 幂等键 → 身份/会话/会话 → 确定性业务效果(建单/事件/运维工作项/确认/命令动作)
  → processing state = AGENT_PENDING
  COMMIT

Agent Run(事务之间)
  LLM + 只读工具,无 DB 写锁(AC-A11)

事务 B(CAS 守卫,恰好一次)
  advance(AGENT_PENDING→AGENT_COMPLETED) → 持久化决策/通知/提案
  → state = COMPLETED
  COMMIT → post-commit dispatch outbox
```

崩溃安全:`AGENT_PENDING / FAILED_RETRYABLE` 可由重复投递**恢复**(resume 只重跑 Agent 阶段,绝不重建工单/事件);`COMPLETED` 重复投递为 no-op。处理状态机:`RECEIVED → AGENT_PENDING → AGENT_COMPLETED → COMPLETED`,`FAILED_RETRYABLE` 可恢复。

### 确定性降级

LLM 不可用 / 超时 / 非法 JSON / 枚举越界 / 超长回复 / 工具被拒 → 全部安全降级到确定性规则分类,业务永不卡死、异常永不外泄。

---

## 消息处理流程图

### 入站流程(每条消息,V2.1 两阶段)

```mermaid
flowchart TD
    M["渠道 webhook 消息<br/>(企业微信/飞书)"] --> V{"Adapter 验签?"}
    V --"失败"--> REJ["HTTP 400 / 忽略<br/>(challenge 请求则返回验证串)"]
    V --"通过"--> E["InboundEnvelope<br/>(message_id 幂等键)"]
    E --> I{"Idempotency<br/>claim 原子成功?"}
    I --"重复且已 COMPLETED"--> DUP["跳过,返回已处理"]
    I --"重复但 AGENT_PENDING<br/>/ FAILED_RETRYABLE"--> RES["resume:只重跑 Agent 阶段<br/>(不重建工单/事件)"]
    I --"首次"--> ID["Canonical Identity<br/>(并发首次消息 0 500)"]
    ID --> CONV["Conversation<br/>查找/兜底注册"]
    CONV --> PUR{"Conversation Purpose"}
    PUR -->|REQUESTER| RQ["确定性预路由:<br/>FAQ / 报修 / 补充 / 查询 / 确认"]
    PUR -->|OPERATOR| OP["斜杠命令:<br/>/claim /resolve /escalate"]
    PUR -->|APPROVAL| AP["/approve /reject"]
    RQ --> WF["Workflow prepare(事务 A)<br/>确定性效果 + AGENT_PENDING"]
    WF -->|需要 Agent| AG["SupportAgent 运行<br/>(事务之间,无写锁)"]
    WF -->|无需 Agent| BIZ
    AG --> APPLY["Workflow apply(事务 B)<br/>CAS 恰好一次<br/>决策持久化 + 通知 + 提案"]
    APPLY --> BIZ["Ticket 变更 +<br/>TicketEvent +<br/>Outbox 记录"]
    BIZ --> CM["事务提交后 dispatch 通知"]
    CM --> OUT["渠道出站(官方请求形状)"]
```

### 用户视角黄金路径(离线 Demo,含 V2.1 多轮/紧迫度/记忆)

```text
 ┌──────────────┐    ┌──────────────┐    ┌───────────────┐
 │ 张三(用户群)   │    │ 李师傅(运维群) │    │ 张三(飞书私聊) │
 └──────┬───────┘    └──────┬───────┘    └──────┬───────┘
        │ 1.A3空调坏了       │                    │
        ▼                   │                    │
 2.user_001 3.T0001 建单    │                    │
 4.Agent 决策→群回执 5.私聊详情│                    │
 6.运维工单                  │                    │
        │ 7."下午领导要来,很急"│                    │
        ▼                   │                    │
 Agent 识别紧迫度↑ → 优先级 P2 + 运维更新提示         │
        │                   │ 8./claim T0001     │
        │                   ▼                    │
        │                  9.原子认领成功          │
 10.接单通知◄────────────────┤                    │
        │                   │ 11./resolve T0001   │
 12.确认请求◄───────────────┘                    │
        │                    │                   │ 13.跨渠道确认
        ▼                    ▼                   ▼
        │                  14.CLOSED 15.记忆抽取 │
        │                  16.新会话"A3空调又坏了" │
        │◄───────────17.记忆召回→memory_refs─────┘
```

---

## 工单状态机

```text
                    ┌───────────────┐
                    ▼               │
 ┌──────┐  claim  ┌──────────┐  resolve ┌──────────┐  confirm  ┌──────┐
 │ OPEN │ ──────▶ │IN_PROGRESS│ ───────▶ │ RESOLVED │ ────────▶ │CLOSED│
 └──────┘         └──────────┘          └──────────┘           └──────┘
     ▲                  │                    │  ▲
     └──── force_close ─┘                    └──┴── reject(用户说"还没好")
              (需原因+审批)                      resolution_rejected
```

| 迁移 | 动作 | 说明 |
| --- | --- | --- |
| `OPEN → IN_PROGRESS` | `claim` | 原子化:`WHERE status='OPEN' AND assignee_user_id IS NULL` + rowcount 校验,25 并发仅 1 胜 |
| `IN_PROGRESS → RESOLVED` | `resolve` | 进入"等待用户确认",**不自动关闭** |
| `RESOLVED → CLOSED` | `confirm` | 用户确认(可跨渠道),触发记忆抽取 |
| `RESOLVED → IN_PROGRESS` | `reject` | 用户驳回,产生 `resolution_rejected` 事件,不建新单 |
| `IN_PROGRESS → CLOSED` | `force_close` | 高风险动作:必须带原因 + 走审批 |

每个迁移同事务写入 `TicketEvent`,且带审计上下文:`actor_user_id`(谁)+ `trace_id`(哪条链路)+ `conversation_id`(在哪个会话)。

---

## 协作与通知链路

### 建单即三面输出(同一个 `ticket_created` 业务事件)

```mermaid
flowchart LR
    E["ticket_created 业务事件<br/>(同事务 3 条 Outbox 记录)"] --> A["① 用户群公开回执<br/>PUBLIC · REACTIVE_REPLY<br/>'张三,已受理,工单 T0001…'"]
    E --> B["② 私聊详情<br/>PRIVATE · PRIVATE_DETAIL<br/>工单号/问题/状态/优先级"]
    E --> C["③ 运维工单<br/>INTERNAL · OPERATOR_WORK_ITEM<br/>优先级/队列/地点/报修人/Agent 建议/CLAIM 指引"]
```

### 通知流水线(Business Event → Channel)

```text
Business Event
      ↓
Notification Policy(事件→通知类型)
      ↓
Notification Type(表达"为什么发",与平台无关)
      ↓
Audience / Visibility(PUBLIC | PRIVATE | INTERNAL)
      ↓
Target Resolver(用户群 / 私聊 / 运维群 / 操作发生会话 / 审批会话)
      ↓
Outbound Message(官方请求形状)
      ↓
Channel Transport(记录型,离线)
```

### 通知类型

| 类型 | 可见性 | 受众 | 示例 |
| --- | --- | --- | --- |
| `REACTIVE_REPLY` | PUBLIC | 用户群 | 已受理回执 |
| `PRIVATE_DETAIL` | PRIVATE | 报修人 DM | 工单详情 |
| `REQUESTER_STATUS_UPDATE` | PUBLIC/PRIVATE | 报修人 | 已由李师傅接手 |
| `OPERATOR_WORK_ITEM` | INTERNAL | 运维群 | 新工单 + CLAIM 指引 |
| `OPERATOR_ACTION_RECEIPT` | INTERNAL | 操作发生会话 | 认领成功回执 |
| `REQUESTER_CONFIRMATION_REQUEST` | PUBLIC/PRIVATE | 报修人 | 请确认是否恢复 |
| `APPROVAL_REQUEST` | INTERNAL | 审批会话 | 升级审批请求 |
| `APPROVAL_RESULT` | INTERNAL | 运维/发起人 | 审批结果 |
| `INTERNAL_NOTE` | INTERNAL | 运维/审批 | 内部备注 |

**去重**:`UNIQUE(source_event_id, notification_type, target)`,同一业务事件不会给同一目标发两遍。
**可靠**:Ticket 变更 + TicketEvent + Outbox 记录同事务提交;渠道发送在提交后执行,失败保留重试(≤3 次)。

### HITL 执行链

```text
ESCALATE T0001(运维群,显式单号)
      ↓
Policy 校验(动作白名单:escalate | force_close)
      ↓
PendingAction 落库(PENDING) + Approval(PENDING,独立状态机)
      ↓
审批群 /approve apr_xxx(CAS 原子,rowcount 守卫)
      ↓
APPROVED → Action Executor 确定性执行(恰好一次)
      ↓
Business Effect(工单 + escalated 事件 + 审计 actor)
      ↓
Notification(通知各方)
```

---

## 技术栈

| 层 | 选型 |
| --- | --- |
| 语言 | Python ≥ 3.12(现代特性:dataclass / type hints / `from __future__ import annotations`) |
| Web | FastAPI ≥ 0.110 + Uvicorn |
| 校验 | Pydantic ≥ 2.7 |
| 存储 | SQLite(单文件,`SerializedConnection` 串行化 + 嵌套安全 `txn()`) |
| 协议加密 | `cryptography` ≥ 42(AES-256-CBC,Feishu/WeCom 官方加密回调) |
| 检索 | 确定性 TF 检索(`Retriever`,FAQ 语料,无向量库) |
| LLM | 可选(OpenRouter),有状态 Agent 语义理解 + 结构化决策;未配置 / 超时 / 非法输出**自动降级为确定性规则** |
| 测试 | pytest ≥ 8(unit / integration / acceptance / concurrency / protocol contract) |
| 部署 | 单体 FastAPI 进程,零外部依赖(未来换 Redis/Kafka 前的刻意取舍) |

## 目录结构

```text
.
├── AGENTS.md                     # 智能体协作约定(只读)
├── README.md                     # 本文件
├── v2.md                         # V2 实现规格(AC-11~30 全量要求)
├── pyproject.toml
├── app/
│   ├── main.py                   # FastAPI 装配:build_core/build_ops/build_ingress/create_app
│   ├── domain/                   # 领域模型(与基础设施零耦合)
│   │   ├── envelope.py           #   InboundEnvelope
│   │   ├── identity.py           #   User / ChannelIdentity / Session
│   │   ├── conversation.py       #   Conversation(Type/Purpose)
│   │   ├── role.py               #   Role(requester|operator|approver)
│   │   ├── ticket.py             #   Ticket / TicketEvent / 状态机
│   │   ├── approval.py           #   Approval(独立状态机)
│   │   ├── pending_action.py     #   PendingAction(HITL 待执行动作)
│   │   ├── notification.py       #   NotificationType / Visibility / Outbox
│   │   ├── outbound.py           #   ChannelCapability / DeliveryTarget / Message
│   │   ├── memory.py · message.py · trace.py
│   ├── application/              # 服务层(业务编排,全部确定性)
│   │   ├── identity_service.py   #   并发安全身份解析
│   │   ├── conversation_service.py · role_service.py
│   │   ├── ticket_service.py     #   TicketResolver(显式单号→续单/建单)
│   │   ├── ticket_action_service.py # 动作执行器 + HITL _execute
│   │   ├── notification_service.py # 事务性 Outbox + dispatch(重试)
│   │   ├── target_resolver.py · command_parser.py
│   │   ├── workflow.py           #   purpose 路由 + prepare/run_agent/apply 两阶段
│   │   ├── ingress_service.py    #   原子幂等入口 + 处理状态机
│   │   ├── intent_router.py · retriever.py · context_builder.py
│   │   ├── support_agent.py      #   V2.1 有状态 Agent(全感知→只读工具→决策)
│   │   ├── agent_decision.py     #   AgentDecision schema + validate_decision
│   │   ├── agent_tools.py        #   只读工具端口(≤2 次/run,白名单)
│   │   ├── policy.py             #   PolicyValidator(提案→HITL 门禁)
│   │   ├── prompt_registry.py · prompts/  # 版本化安全渲染 Prompt 模板
│   │   ├── memory_service.py · memory_extractor.py
│   │   ├── approval_service.py · session_service.py
│   ├── adapters/                 # 渠道协议(严禁触碰业务)
│   │   ├── base.py               #   ChannelAdapter 协议 + VerificationError
│   │   ├── feishu.py             #   官方事件/URL 验证/AES
│   │   ├── wecom.py              #   官方签名/AES/XML/能力诚实声明
│   │   ├── outbound.py           #   官方出站请求构造
│   │   └── transports.py         #   HttpTransport(记录/fail_next) + RealHttpTransport
│   └── infrastructure/           # 基础设施
│       ├── db.py                 #   SerializedConnection(RLock)+ apply_migrations
│       ├── idempotency.py        #   原子幂等(同事务 claim)
│       ├── processing.py         #   InboundProcessingStore(崩溃可恢复状态机)
│       ├── repositories.py       #   txn() + 全部仓储
│       └── llm_client.py         #   可选 LLM,降级安全
├── storage/migrations/           # 0001~0014(含 V2 的 0009~0012,V2.1 的 0013~0014)
├── seed/
│   ├── faq/                      # FAQ 语料(检索/评估)
│   └── conversations.json        # 演示会话注册(4 个群)
├── tests/                        # 235 个测试(详见测试章节)
├── docs/                         # 全部设计文档
└── reference/                    # 旧项目只读快照(gitignored)
```

---

## 领域模型

```mermaid
erDiagram
    USER ||--o{ CHANNEL_IDENTITY : "渠道身份"
    USER ||--o{ SESSION : "会话"
    USER ||--o{ ROLE : "角色"
    USER ||--o{ TICKET : "我的工单"
    USER ||--o{ MEMORY : "长期记忆"
    CONVERSATION ||--o{ SESSION : "产生"
    TICKET ||--o{ TICKET_EVENT : "审计"
    TICKET ||--o{ NOTIFICATION : "通知"
    TICKET ||--o{ PENDING_ACTION : "HITL"
    PENDING_ACTION ||--o| APPROVAL : "审批"
    NOTIFICATION ||--o| OUTBOX : "投递"

    USER {
        string id PK "user_001"
        string display_name "张三"
    }
    CHANNEL_IDENTITY {
        string channel "wecom|feishu"
        string channel_user_id "zhangsan"
    }
    CONVERSATION {
        string channel "wecom"
        string channel_conversation_id "repair_group_1"
        string purpose "REQUESTER|OPERATOR|APPROVAL"
        string conversation_type "DM|GROUP"
        string queue "facility"
    }
    TICKET {
        string id PK "T0001"
        string status "OPEN|IN_PROGRESS|RESOLVED|CLOSED"
        string assignee_user_id "user_ops_001"
        string summary "持久化摘要"
        string source_conversation_id "repair_group_1"
    }
    TICKET_EVENT {
        string event_type "claimed|resolved|escalated|..."
        string actor_user_id "user_ops_001"
        string trace_id "trace_xxx"
    }
    APPROVAL {
        string status "PENDING|APPROVED|REJECTED"
        string action "escalate|force_close"
    }
```

---

## 渠道协议

> 原则:**Mock 网络,不能 Mock 协议。** 所有实现能力都记录在 `docs/CHANNEL_PROTOCOL_MATRIX.md`(含官方文档 URL 与 2026-08-13 验证日期);官方无法证明的能力一律 `UNSUPPORTED / PENDING_OFFICIAL_SPEC`。

| 能力 | Feishu | WeCom |
| --- | --- | --- |
| `DM_INBOUND` | ✅ `im.message.receive_v1`(`message_id` 幂等) | ✅ 文本消息回调(`MsgId` 幂等,AES-256-CBC) |
| `GROUP_INBOUND` | ✅ `chat_type=group` | ⛔ `PENDING_OFFICIAL_SPEC`(官方文本消息格式无 chat_id) |
| `DM_OUTBOUND` | ✅ `POST /open-apis/im/v1/messages?receive_id_type=open_id` | ✅ `POST /cgi-bin/message/send`(`touser`) |
| `GROUP_OUTBOUND` | ✅ `receive_id_type=chat_id` | ✅ `POST /cgi-bin/appchat/send`(`chatid`) |
| `WEBHOOK_VERIFICATION` | ✅ token / challenge / Encrypt Key | ✅ `sha1(sort(token,timestamp,nonce,encrypt))` / echostr / AES |

**出站请求契约测试**(RecordingTransport 断言):

```text
Feishu DM :  URL=https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id
             Authorization: Bearer <tenant_access_token>
             body={receive_id, msg_type:"text", content:'{"text":"…"}', uuid}
WeCom DM  :  URL=https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=…
             body={touser, msgtype:"text", agentid, text:{content}}
WeCom 群  :  URL=…/cgi-bin/appchat/send?access_token=…
             body={chatid, msgtype:"text", text:{content}}
```

**离线默认值**:`REAL_CHANNEL_NETWORK` 未设置 → 使用记录型 `HttpTransport`,所有 Demo/测试零凭证、零外网。启用真实联网只需设置该变量 + 配置凭据(见下文"真实渠道接入")。

---

## API 一览

### 消息入口

| 方法 | 端点 | 说明 |
| --- | --- | --- |
| `GET/POST` | `/webhooks/wecom` | WeCom 验签(`msg_signature` sha1 + AES 解密 + XML);GET 返回 echostr 验证串 |
| `GET/POST` | `/webhooks/feishu` | Feishu token 校验 / `url_verification` challenge / Encrypt Key 解密;消息按官方形状解析 |
| `POST` | `/webhooks/{channel}` | 统一入站:`message_id` 原子幂等 → 身份 → purpose 路由 → 业务 + 通知 |

### 会话与权限

| 方法 | 端点 | 说明 |
| --- | --- | --- |
| `POST` | `/conversations/register` | 注册会话:`{channel, channel_conversation_id, purpose, type, queue, location}` |
| `GET` | `/conversations` | 列出已注册会话(purpose 路由依据) |

### 工单与协作

| 方法 | 端点 | 说明 |
| --- | --- | --- |
| `POST` | `/tickets/{id}/claim` | 确定性认领(REST,需 operator 角色,见信任边界) |
| `POST` | `/tickets/{id}/resolve` | 解决 → 等待用户确认(需 operator 角色) |
| `POST` | `/tickets/{id}/close` | **DEPRECATED(V2.1)**:无审批直接关闭已移除;必须带 `reason`,走 FORCE_CLOSE 审批流水线 |
| `POST` | `/tickets/{id}/escalate` | 发起升级审批(返回 `approval_id`) |
| `POST` | `/approvals/{id}/approve` · `/reject` | 审批决策(需 approver 角色,幂等 CAS) |
| `GET` | `/approvals` | 审批列表(独立状态机) |
| `GET` | `/tickets/{id}/case` | **全案追踪**:事件(actor/trace)+ 通知 + 投递尝试 + 审批 + pending actions + 记忆 + 工单 |

> **REST 信任边界(V2.1)**:控制面 API 不再信任任意 actor 字符串。每个动作必须携带 `{"actor_user_id": "user_xxx"}` 或 `{"actor": {"channel": "wecom", "channel_user_id": "lihua"}}`,系统先解析规范 actor、再校验角色(claim/resolve 需 operator,approve/reject 需 approver),不存在或角色不符返回 401/403。

### 查询

| 方法 | 端点 | 说明 |
| --- | --- | --- |
| `GET` | `/memories?user_id=&kind=` | 长期记忆(CLOSED 工单抽取;recall 排序消费 confidence) |
| `GET` | `/traces/{trace_id}` | 单条消息全链路(channel→identity→intent→ticket/agent/retrieval→reply;agent 阶段含 prompt 版本/模型/时延/工具/refs/fallback,**不落原始 prompt**) |
| `GET` | `/health` | 健康检查 |

> **单一 Ticket API**:不存在第二套生命周期接口;斜杠命令、REST、webhook 都只是 `TicketAction` 的输入适配器。

---

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                 # 235 passed(离线、确定性)
uvicorn app.main:app   # http://127.0.0.1:8000
```

可选 LLM(OpenRouter,`.env` 配置,已被 gitignore):

```bash
LLM_API_KEY=...        # Agent 语义理解 + 结构化决策
LLM_BASE_URL=...       # 默认 https://openrouter.ai/api/v1
LLM_MODEL=...          # 默认 nvidia/nemotron-3-ultra-550b-a55b:free
```

> 未配置 / 超时 / 非法输出自动降级为确定性规则,永不阻塞主流程。
> 默认测试**永远离线**:即使 shell 导出了 `REAL_CHANNEL_NETWORK=true`,普通 `pytest` 也强制走记录型 transport;真实联网测试必须显式 `RUN_REAL_CHANNEL_TESTS=1`。

## 一键演示

`seed/conversations.json` 预注册了 4 个会话(完整离线):

```json
[
  {"channel": "wecom", "channel_conversation_id": "repair_group_1", "purpose": "REQUESTER",  "type": "GROUP", "queue": "facility", "location": "A3"},
  {"channel": "wecom", "channel_conversation_id": "op_group_facility", "purpose": "OPERATOR", "type": "GROUP", "queue": "facility"},
  {"channel": "wecom", "channel_conversation_id": "approval_room",   "purpose": "APPROVAL",  "type": "GROUP"},
  {"channel": "feishu", "channel_conversation_id": "oc_op_facility", "purpose": "OPERATOR", "type": "GROUP", "queue": "facility"}
]
```

```bash
# 1. 用户报修群:建单 T0001(群回执 + 私聊详情 + 运维工单 三面输出)
curl -s -X POST http://127.0.0.1:8000/webhooks/wecom -H 'Content-Type: application/json' \
  -d '{"MsgId":"m1","FromUserName":"zhangsan","Content":"A3 空调坏了","CreateTime":1000,"conversation_id":"repair_group_1"}'

# 2. 运维群显式认领(无隐式 active ticket)
curl -s -X POST http://127.0.0.1:8000/webhooks/wecom -H 'Content-Type: application/json' \
  -d '{"MsgId":"m2","FromUserName":"lihua","Content":"/claim T0001","CreateTime":2000,"conversation_id":"op_group_facility"}'

# 3. 解决 → 等待用户确认
curl -s -X POST http://127.0.0.1:8000/webhooks/wecom -H 'Content-Type: application/json' \
  -d '{"MsgId":"m3","FromUserName":"lihua","Content":"/resolve T0001 已更换空调滤网","CreateTime":3000,"conversation_id":"op_group_facility"}'

# 4. 用户从绑定的飞书 DM 跨渠道确认 → CLOSED → 记忆抽取
curl -s -X POST http://127.0.0.1:8000/webhooks/feishu -H 'Content-Type: application/json' \
  -d '{"event_id":"e1","event":{"message":{"message_id":"om1","chat_type":"p2p","content":"{\"text\":\"T0001 已恢复\"}"},
        "sender":{"sender_id":{"open_id":"ou_zhangsan"}},"chat_id":"oc_dm"}}'

# 5. 新会话记忆召回
curl -s -X POST http://127.0.0.1:8000/webhooks/wecom -H 'Content-Type: application/json' \
  -d '{"MsgId":"m5","FromUserName":"zhangsan","Content":"空调又坏了","CreateTime":5000,"conversation_id":"conv_new"}'

# 6. 全案追踪(actor/trace/通知/记忆)
curl -s http://127.0.0.1:8000/tickets/T0001/case
```

完整 16 步场景(含 V2.1 多轮续单 / 紧迫度升级 / 记忆召回)在 `tests/test_demo_v2.py::test_demo_v2_full_golden_path`。Agent 决策演示脚本(FAQ grounded 回答、低置信转人工真实建单、Agent 提案→审批→确定性执行)见 `test_demo_faq_rag` / `test_demo_unknown_knowledge_real_handoff` / `test_demo_agent_proposes_hitl_executes`。

---

## 测试与质量指标

### 测试矩阵

| 类型 | 文件 | 覆盖 |
| --- | --- | --- |
| unit / integration | `test_*.py`(V1) | 领域/状态机/仓储/服务/适配器 |
| acceptance | `test_golden_path.py` · `test_v2_collaboration.py` | AC-01~AC-21 |
| concurrency | `test_v2_concurrency.py` | AC-15 / AC-25 / 身份并发 / 幂等释放 |
| HITL + 通知 | `test_v2_hitl_notifications.py` | AC-22~24 + outbox 存活 |
| protocol contract | `test_v2_protocol.py` | AC-26~28(14 个:官方形状/验签/AES/出站契约) |
| demo | `test_demo_v2.py` | 升级版黄金路径 + FAQ RAG + 低置信转人工 + Agent 提案 HITL + Case Trace |
| **agent core (V2.1)** | `test_agent_core.py` | AC-A01~A18 + prompt injection + 恶意模型不可改状态 |
| **agent eval (V2.1)** | `test_agent_eval.py` | AC-A20 golden set(14 例,100% 通过率) |
| **processing (V2.1)** | `test_processing_state.py` | AC-A11~A14 + 并发重复恰好一次 |
| **prompt registry (V2.1)** | `test_prompt_registry.py` | 版本/加载/变量校验/字面大括号/schema |

### 质量指标(测试实测,非预设)

| 指标 | 目标 | 实测 |
| --- | --- | --- |
| 全量测试 | — | **235 passed / 0 failed** |
| Golden Path AC-01~AC-10 | 全绿 | 10/10 |
| V2 验收 AC-11~AC-30 | 全绿 | 20/20 |
| V2.1 Agent AC-A01~AC-A20 | 全绿 | 20/20 |
| Agent golden eval set | 10+ 例 | **14/14(100%)** |
| FAQ 检索 Recall@3 | ≥ 90% | **100%**(14/14) |
| 记忆抽取 Precision | ≥ 85% | **100%**(11/11) |
| 记忆 confidence 参与排序 | 是 | 相同相关性按 confidence 排序(测试) |
| LLM 延迟不占写锁 | 是 | SlowLLM 0.6s 期间锁可被其他事务获取 |
| 崩溃恢复(phase A 后) | 无重复工单/事件 | resume 测试通过 |
| 并发重复 webhook(10 线程,agent 路径) | 1 执行 / 0 500 | 1 执行 / 0 重复 |
| 并发重复 webhook(25 线程) | 1 执行 / 0 500 | 1 执行 / 0 重复 / 0 500 |
| 并发认领(25 线程) | exactly 1 winner | 1 胜 / 1 claimed 事件 |
| 通知去重 | 每(事件,类型,目标)1 条 | 1 条 |
| 模拟投递失败 | 业务不丢 / 可重试 | 保留 + 重试成功 |
| 真实联网 / 真实凭证 | 不需要 | 未使用(默认强制离线) |

---

## 真实渠道接入(未来)

当前所有 Demo/测试默认离线(`REAL_CHANNEL_NETWORK` 未设置)。未来接入真实渠道:

```bash
# 企业微信
export WECOM_CORP_ID=... WECOM_CORP_SECRET=... WECOM_AGENT_ID=...
export WECOM_TOKEN=... WECOM_ENCODING_AES_KEY=...     # 回调验签/解密
# 飞书
export FEISHU_APP_ID=... FEISHU_APP_SECRET=...
export FEISHU_VERIFICATION_TOKEN=... FEISHU_ENCRYPT_KEY=...
# 真实出站
export REAL_CHANNEL_NETWORK=true
```

然后配置平台回调 URL 指向 `/webhooks/{channel}`,提供真实 conversation ids 并注册。**只需 config + transport,无需重设计** Identity / Conversation / Ticket / Notification / Workflow / Agent。

> 注意:`.env` 中即使配置了 `REAL_CHANNEL_NETWORK=true` 与真实凭据,普通 `pytest` 仍不会联网——测试套件在 import 时强制离线,真实联网测试需 `RUN_REAL_CHANNEL_TESTS=1` 显式开启。

当前唯一能力缺口:WeCom `GROUP_INBOUND`(官方文档未证明群消息回调携带 chat_id,标 `PENDING_OFFICIAL_SPEC`)。

---

## 文档

| 文档 | 内容 |
| --- | --- |
| `docs/PRODUCT_SCOPE.md` | 产品范围(做什么 / 不做什么) |
| `docs/ARCHITECTURE.md` | 架构、分层与 11 条核心不变量 |
| `docs/DOMAIN_MODEL.md` | 领域实体、状态机、事务约束 |
| `docs/GOLDEN_PATH.md` | 黄金路径与里程碑 |
| `docs/ACCEPTANCE_TESTS.md` | 验收契约 AC-01 ~ AC-30 |
| `docs/CHANNEL_PROTOCOL_MATRIX.md` | **渠道协议证据矩阵**(官方 URL + 字段 + 幂等 + 能力状态) |
| `docs/V2_IMPLEMENTATION_REPORT.md` | **V2 实现报告**(schema/模型/指标/遗留) |
| `docs/V2_1_AGENT_CORE_IMPLEMENTATION_REPORT.md` | **V2.1 Agent Core 实现报告**(before/after、决策 schema、工具/事务边界、安全不变量、eval 结果) |
| `docs/DEVELOPMENT_PLAN.md` | 分阶段开发计划 |
| `docs/IMPLEMENTATION_BACKLOG.md` | 已完成 / 未来工作清单 |
| `docs/LEGACY_PORT_MAP.md` | 旧代码移植 / 改写 / 忽略对照 |
| `docs/HANDOVER.md` | 会话交接 |
| `V1_TO_V2_ARCHITECTURE_AUDIT.md` | V1→V2 只读审计 |
| `docs/reference/LEGACY_ARCHITECTURE_AUDIT.md` | 旧项目架构审计(仅参考) |
| `v2.md` | V2 实现规格(任务原文) |
