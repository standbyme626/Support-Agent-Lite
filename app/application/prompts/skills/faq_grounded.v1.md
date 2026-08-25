---
key: faq_grounded
version: 1
applies_to: faq_answer, faq
not_applies_to: chitchat
requires: -
failure_mode: fallback_main
output_format: markdown, 引用来源id
---
## summary
证据式知识库问答：只依据 knowledge_block 回答并引用 source_id。
## full
话术模板：
【{source_id} {title}】结论要点（来源：doc_id 标题）。
边界：knowledge_block 为「（无）」或证据分数低 → recommended_action=ask_clarification 或 assign_operator，绝不编造。
示例：用户问 VPN 配置 → 引用 faq-007 全文要点。
