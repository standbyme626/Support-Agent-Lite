# GOLDEN_PATH

The end-to-end path every new feature must serve. If a capability does not serve this path: not now.

```text
企业微信 / 飞书
        ↓
Channel Adapter (Raw → InboundEnvelope)
        ↓
Canonical Identity (channel_user_id → user)
        ↓
Session (belongs to user)
        ↓
Intent Router (FAQ | Support | ProgressQuery | Other)
      /        \
   FAQ         Support
    ↓             ↓
   RAG          Ticket Resolver (create / continue)
    ↓             ↓
 grounded      Context Builder
 answer        ↓
 (no ticket)   Agent (summary / recommendation only)
                ↓
             Workflow
                ↓
           Human Operator (claim / resolve / close)
                ↓
           High-risk Action → Approval (independent)
                ↓
           Resolve / Close
                ↓
           Memory Extraction (stable facts)
                ↓
           Next Session Recall
```

## Milestones

1. **Milestone 1 (identity core)**: `wecom/zhangsan → user_001 → create T1001`, then `feishu/ou_001 → user_001 → continue T1001`, verify NO T1002.
2. **Milestone 2 (intent split)**: FAQ → RAG → answer (no ticket); Support → ticket. Knowledge workflow and business workflow are separate.
3. **Milestone 3 (closure loop)**: Ticket → Operator → Approval → Close → Memory → New Session Recall.
