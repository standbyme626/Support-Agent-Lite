# Support Agent Lite

跨渠道企业支持代理(Cross-Channel Enterprise Support Agent)。

以**用户为中心(User-Centric)、工作流优先(Workflow-First)**的架构重新实现,汲取旧项目 `support-agent-platform` 的经验教训(旧项目以只读方式保存在 `reference/`)。

## 当前状态

Phase 2 — 身份与工单核心(Identity + Ticket Core):`IdentityResolver`(渠道身份 → 规范用户)、`TicketResolver`(显式单号 → 会话单号 → 活跃单 → 澄清)、`TicketService` 已就绪,AC-05 跨渠道续单与 AC-06 多单澄清已由测试覆盖。

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
uvicorn app.main:app
```

## 文档

- `docs/PRODUCT_SCOPE.md` — 产品范围(做什么 / 不做什么)
- `docs/ARCHITECTURE.md` — 架构与核心不变量
- `docs/DOMAIN_MODEL.md` — 领域实体与状态机
- `docs/GOLDEN_PATH.md` — 系统黄金路径
- `docs/ACCEPTANCE_TESTS.md` — 验收契约(AC-01 ~ AC-10)
- `docs/DEVELOPMENT_PLAN.md` — 分阶段开发计划
- `docs/LEGACY_PORT_MAP.md` — 旧代码移植 / 改写 / 忽略对照表
- `docs/reference/LEGACY_ARCHITECTURE_AUDIT.md` — 旧项目架构审计(仅参考)