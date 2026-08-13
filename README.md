# Support Agent Lite

跨渠道企业支持代理(Cross-Channel Enterprise Support Agent)。

以**用户为中心(User-Centric)、工作流优先(Workflow-First)**的架构实现:企业微信 / 飞书消息 → 规范用户身份 → Conversation Purpose 路由 → FAQ 直答(不建单)或自动建单 → 三面协作(用户群回执 / 私聊详情 / 运维工单)→ 人工协作(CLAIM/RESOLVE/ESCALATE/HITL 执行)→ 用户确认关闭 → 记忆抽取 → 新会话召回。完整闭环,可本地跑通、面试可展示。

> 旧项目 `support-agent-platform` 以只读方式保存在 `reference/`,仅作参考,禁止修改/提交。

## 当前状态

V1(7 个 Phase)与 **V2(Full Collaboration Layer)均已完成**,**178 个测试全绿**:

- V1:AC-01 ~ AC-10 全部通过(139 测试基线,提交 `8627b0f`)
- V2:AC-11 ~ AC-30 全部通过(41 个 V2 测试:协作层 12 · 并发 4 · HITL/通知 6 · 协议契约 14 · 离线 Demo 5)

| Phase | 内容 | 提交 |
| --- | --- | --- |
| 0 | 项目骨架:FastAPI / pytest / 迁移 / `/health` | `c029788` |
| 1 | 领域实体 + 严格状态机 + 事务性 Ticket+Event | `6018685` |
| 2 | IdentityResolver / SessionService / TicketService / TicketResolver | `34f197e` |
| 3 | WeCom/Feishu 适配器 + webhook + 幂等 | `508f8a2` |
| 4 | IntentRouter / RAG Retriever / ContextBuilder / SupportAgent / Workflow | `070f193` |
| 5 | Operator API(claim/resolve/close/escalate)+ Approval 状态机 | `d598e76` |
| 6 | Memory:CLOSED 抽取稳定事实 + 新会话召回 | `a6e337d` |
| 7 | Trace(全链路 trace_id)+ 10 条 Golden Path + 本 Demo | `8627b0f` |
| **V2** | **Conversation(Purpose/Type)/ Role / Operator 认领 / 三面可见性 / 通知 outbox / 出站契约 / 确认关闭 / HITL 执行 / 并发修复** | 见 `docs/V2_IMPLEMENTATION_REPORT.md` |

## 核心不变量

1. 渠道身份 ≠ 规范用户
2. Session ≠ 用户
3. Session ≠ 记忆
4. Agent 不得直接改动敏感工单状态(只出建议)
5. 工单状态与 TicketEvent 必须同事务提交
6. Approval 是独立状态机(工单不受 PENDING 影响)
7. 低置信 RAG 不得变成自由发挥的模型答案
8. 跨渠道续单必须经规范用户身份解析
9. **Channel ≠ Role**(同一渠道可同时存在 Requester/Operator/Approver Conversation)
10. **共享 Operator 群无隐式 active ticket**(动作必须显式带工单号)
11. **Mock network, not protocol**(协议契约严格依据官方文档,无法证明的能力标 `UNSUPPORTED/PENDING_OFFICIAL_SPEC`)

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                 # 178 passed
uvicorn app.main:app   # http://127.0.0.1:8000
```

## 一键演示(V2 全流程:建单 → 三面输出 → 认领 → 解决 → 跨渠道确认 → 关闭)

```bash
# 1. 用户在报修群发消息 → 建单 T0001,同时产生:群回执 + 私聊详情 + 运维工单
curl -s -X POST http://127.0.0.1:8000/webhooks/wecom -H 'Content-Type: application/json' \
  -d '{"MsgId":"m1","FromUserName":"zhangsan","Content":"A3 空调坏了","CreateTime":1000,"conversation_id":"repair_group_1"}'

# 2. 运维人员在运维群显式认领(无隐式 active ticket)
curl -s -X POST http://127.0.0.1:8000/webhooks/wecom -H 'Content-Type: application/json' \
  -d '{"MsgId":"m2","FromUserName":"lihua","Content":"/claim T0001","CreateTime":2000,"conversation_id":"op_group_facility"}'

# 3. 运维解决 → 等待用户确认(不是直接关闭)
curl -s -X POST http://127.0.0.1:8000/webhooks/wecom -H 'Content-Type: application/json' \
  -d '{"MsgId":"m3","FromUserName":"lihua","Content":"/resolve T0001 已更换空调滤网","CreateTime":3000,"conversation_id":"op_group_facility"}'

# 4. 用户从绑定的飞书 DM 跨渠道确认 → CLOSED → 记忆抽取
curl -s -X POST http://127.0.0.1:8000/webhooks/feishu -H 'Content-Type: application/json' \
  -d '{"event_id":"e1","event":{"message":{"message_id":"om1","chat_type":"p2p","content":"{\"text\":\"T0001 已恢复\"}"},
        "sender":{"sender_id":{"open_id":"ou_zhangsan"}},"chat_id":"oc_dm"}}'

# 5. 高风险动作:ESCALATE → 审批群 APPROVE → 动作真实执行(escalated 事件 + actor 审计)
curl -s -X POST http://127.0.0.1:8000/webhooks/wecom -H 'Content-Type: application/json' \
  -d '{"MsgId":"m4","FromUserName":"lihua","Content":"/escalate T0001 用户要求升级","CreateTime":4000,"conversation_id":"op_group_facility"}'

# 6. 新会话记忆召回 + 全链路 Case Trace(含 actor/trace 审计)
curl -s -X POST http://127.0.0.1:8000/webhooks/wecom -H 'Content-Type: application/json' \
  -d '{"MsgId":"m5","FromUserName":"zhangsan","Content":"空调又坏了","CreateTime":5000,"conversation_id":"conv_new"}'
curl -s http://127.0.0.1:8000/tickets/T0001/case
```

真实 LLM 可选:在 `.env` 配置 `LLM_API_KEY`(OpenRouter)后,Agent 摘要/回复草稿会用 LLM 润色;未配置或超时自动降级为确定性规则,永不阻塞主流程。

## API 一览

| 端点 | 说明 |
| --- | --- |
| `GET/POST /webhooks/{channel}` | 协议入口(Feishu token/challenge/encrypt 校验;WeCom sha1 签名/AES 解密)+ 消息处理 |
| `POST /conversations/register` · `GET /conversations` | Conversation(Purpose/Type)注册与管理 |
| `POST /tickets/{id}/actions` | claim / resolve / force-close / escalate(确定性动作,非法转换 409) |
| `POST /tickets/{id}/approval` | approve / reject(幂等审批,AC-22 执行链) |
| `GET /tickets/{id}/case` | 全案追踪:事件(actor/trace)+ 通知 + 记忆 + 工单 |
| `GET /memories?user_id=&kind=` | 长期记忆(closed 工单抽取) |
| `GET /traces/{trace_id}` | 单条消息全链路追踪 |

## 质量指标(由测试产出)

| 指标 | 目标 | 实测 |
| --- | --- | --- |
| 全量测试 | — | **178 passed** |
| Golden Path AC-01~AC-10 | 全绿 | 10/10 |
| V2 验收 AC-11~AC-30 | 全绿 | **20/20**(41 个测试) |
| FAQ 检索 Recall@3 | ≥ 90% | **100%**(14/14) |
| 记忆抽取 Precision | ≥ 85% | **100%**(11/11) |
| 并发重复 webhook(25 线程) | 1 执行 / 0 500 | 1 执行 / 0 重复 / 0 500 |
| 并发认领(25 线程) | 1 胜出 | 1 胜出 |
| 通知去重 | 每(事件,类型,目标)1 条 | 1 条 |
| 真实联网 / 真实凭证 | 不需要 | **未使用**(RecordingTransport) |

## 文档

- `docs/PRODUCT_SCOPE.md` — 产品范围(做什么 / 不做什么)
- `docs/ARCHITECTURE.md` — 架构与核心不变量
- `docs/DOMAIN_MODEL.md` — 领域实体与状态机
- `docs/GOLDEN_PATH.md` — 系统黄金路径
- `docs/ACCEPTANCE_TESTS.md` — 验收契约(AC-01 ~ AC-30)
- `docs/CHANNEL_PROTOCOL_MATRIX.md` — **渠道协议证据矩阵**(官方 URL + 字段 + 幂等 + 能力状态)
- `docs/V2_IMPLEMENTATION_REPORT.md` — **V2 实现报告**(schema/模型/指标/遗留)
- `docs/DEVELOPMENT_PLAN.md` — 分阶段开发计划
- `docs/LEGACY_PORT_MAP.md` — 旧代码移植 / 改写 / 忽略对照表
- `V1_TO_V2_ARCHITECTURE_AUDIT.md` — V1→V2 架构审计(只读产物)
- `docs/reference/LEGACY_ARCHITECTURE_AUDIT.md` — 旧项目架构审计(仅参考)
