# Support Agent Lite

跨渠道企业支持代理(Cross-Channel Enterprise Support Agent)。

以**用户为中心(User-Centric)、工作流优先(Workflow-First)**的架构实现:企业微信 / 飞书消息 → 规范用户身份 → 意图路由 → FAQ 直答(不建单)或自动建单 → 人工协作(HITL) → 审批 → 关闭 → 记忆抽取 → 新会话召回。完整闭环,可本地跑通、面试可展示。

> 旧项目 `support-agent-platform` 以只读方式保存在 `reference/`,仅作参考,禁止修改/提交。

## 当前状态

7 个 Phase 全部完成,139 个测试全绿,10 条 Golden Path(AC-01 ~ AC-10)全部通过。

| Phase | 内容 | 提交 |
| --- | --- | --- |
| 0 | 项目骨架:FastAPI / pytest / 迁移 / `/health` | `c029788` |
| 1 | 领域实体 + 严格状态机 + 事务性 Ticket+Event | `6018685` |
| 2 | IdentityResolver / SessionService / TicketService / TicketResolver | `34f197e` |
| 3 | WeCom/Feishu 适配器 + webhook + 幂等 | `508f8a2` |
| 4 | IntentRouter / RAG Retriever / ContextBuilder / SupportAgent / Workflow | `070f193` |
| 5 | Operator API(claim/resolve/close/escalate)+ Approval 状态机 | `d598e76` |
| 6 | Memory:CLOSED 抽取稳定事实 + 新会话召回 | `a6e337d` |
| 7 | Trace(全链路 trace_id)+ 10 条 Golden Path + 本 Demo | `(本分支 HEAD)` |

## 核心不变量

1. 渠道身份 ≠ 规范用户
2. Session ≠ 用户
3. Session ≠ 记忆
4. Agent 不得直接改动敏感工单状态(只出建议)
5. 工单状态与 TicketEvent 必须同事务提交
6. Approval 是独立状态机(工单不受 PENDING 影响)
7. 低置信 RAG 不得变成自由发挥的模型答案
8. 跨渠道续单必须经规范用户身份解析

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                 # 139 passed
uvicorn app.main:app   # http://127.0.0.1:8000
```

## 一键演示(Golden Path 全流程)

```bash
# 1. 企业微信 FAQ:意图=faq → RAG 有出处答案 → 不建单
curl -s -X POST http://127.0.0.1:8000/webhooks/wecom -H 'Content-Type: application/json' \
  -d '{"MsgId":"m1","FromUserName":"zhangsan","Content":"年假怎么申请？","CreateTime":1000}'

# 2. 企业微信报修:意图=support → 自动建单 T0001(OPEN + created 事件)
curl -s -X POST http://127.0.0.1:8000/webhooks/wecom -H 'Content-Type: application/json' \
  -d '{"MsgId":"m2","FromUserName":"zhangsan","Content":"A3 空调坏了","CreateTime":2000}'

# 3. 飞书跨渠道续单:ou_001 → 规范用户 → 续 T0001,绝不新建 T0002
curl -s -X POST http://127.0.0.1:8000/webhooks/feishu -H 'Content-Type: application/json' \
  -d '{"event_id":"e1","event":{"message":{"message_id":"om1","text":"昨天空调那个事情怎么样了？"},
        "sender":{"sender_id":{"open_id":"ou_001"}},"chat_id":"oc1"}}'

# 4. 人工认领 → 处理 → 关闭(关闭自动抽取记忆)
curl -s -X POST http://127.0.0.1:8000/tickets/T0001/claim
curl -s -X POST http://127.0.0.1:8000/tickets/T0001/resolve -H 'Content-Type: application/json' \
  -d '{"note":"已更换空调滤网"}'
curl -s -X POST http://127.0.0.1:8000/tickets/T0001/close

# 5. 高风险动作走审批(工单状态不受影响)
curl -s -X POST http://127.0.0.1:8000/tickets/T0001/escalate -H 'Content-Type: application/json' \
  -d '{"reason":"用户要求升级"}'
curl -s http://127.0.0.1:8000/approvals
curl -s -X POST http://127.0.0.1:8000/approvals/apr_xxx/approve -H 'Content-Type: application/json' \
  -d '{"decided_by":"manager"}'

# 6. 新会话记忆召回:空调又坏了 → 召回上次处理结果
curl -s -X POST http://127.0.0.1:8000/webhooks/wecom -H 'Content-Type: application/json' \
  -d '{"MsgId":"m3","FromUserName":"zhangsan","Content":"空调又坏了","CreateTime":3000,"conversation_id":"conv_new"}'

# 7. 全链路追踪:一条消息从 channel → identity → intent → ticket/agent → reply
curl -s http://127.0.0.1:8000/traces/trace_xxx
```

真实 LLM 可选:在 `.env` 配置 `LLM_API_KEY`(OpenRouter)后,Agent 摘要/回复草稿会用 LLM 润色;未配置或超时自动降级为确定性规则,永不阻塞主流程。

## API 一览

| 端点 | 说明 |
| --- | --- |
| `POST /webhooks/{channel}` | 企业微信 / 飞书消息入口(message_id 幂等) |
| `POST /tickets/{id}/claim|resolve|close` | 人工生命周期(非法转换 409) |
| `POST /tickets/{id}/escalate` | 高险动作 → 审批(不改工单状态) |
| `GET /approvals` · `POST /approvals/{id}/approve|reject` | 独立审批状态机 |
| `GET /memories?user_id=&kind=` | 长期记忆(closed 工单抽取) |
| `GET /traces/{trace_id}` | 单条消息全链路追踪 |

## 质量指标(由测试产出)

| 指标 | 目标 | 实测 |
| --- | --- | --- |
| 全量测试 | — | 139 passed |
| Golden Path AC-01~AC-10 | 全绿 | 10/10 |
| FAQ 检索 Recall@3 | ≥ 90% | **100%**(14/14) |
| 记忆抽取 Precision | ≥ 85% | **100%**(11/11) |
| 重复消息 → 重复建单 | 0 | 0(幂等) |
| 跨渠道同人解析 / 同单续接 | 100% | 100% |

## 文档

- `docs/PRODUCT_SCOPE.md` — 产品范围(做什么 / 不做什么)
- `docs/ARCHITECTURE.md` — 架构与核心不变量
- `docs/DOMAIN_MODEL.md` — 领域实体与状态机
- `docs/GOLDEN_PATH.md` — 系统黄金路径
- `docs/ACCEPTANCE_TESTS.md` — 验收契约(AC-01 ~ AC-10)
- `docs/DEVELOPMENT_PLAN.md` — 分阶段开发计划
- `docs/LEGACY_PORT_MAP.md` — 旧代码移植 / 改写 / 忽略对照表
- `docs/reference/LEGACY_ARCHITECTURE_AUDIT.md` — 旧项目架构审计(仅参考)
