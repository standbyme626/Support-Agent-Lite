---
key: diagnosis
version: 1
applies_to: support
not_applies_to: chitchat, progress_query
requires: -
failure_mode: fallback_main
output_format: markdown, 现象/原因/建议 三段
---
## summary
故障诊断建议：结合历史记忆与相似案例给出现象归纳、可能原因、下一步。
## full
话术模板：
现象：{summary}
可能原因：{causes}
建议：先尝试 {quick_fix}；若无效应报修，我会创建工单并同步运维。
边界：只读诊断，绝不声称已执行修复；引用 similar_tickets 时注明案例号。
示例：插U盘蓝屏 → kb-hw-0004 案例 + 驱动排查步骤。
