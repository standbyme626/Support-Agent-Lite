---
key: reply_handoff
version: 1
applies_to: no_answer, handoff
not_applies_to: chitchat, progress_query
requires: approval_granted(仅 force_close 场景)
failure_mode: fallback_main
output_format: markdown, ≤3段, 含下一步指引
---
## summary
转人工沟通：如实告知已转人工，含工单号与状态，不编造细节。
## full
话术模板：
该问题需要人工处理。工单 {ticket_id} 已进入处理队列（状态 {status}），专人会尽快跟进。
边界：不得承诺具体时间；不得声称已解决；force_close 需审批通过后才可执行。
示例：知识库无答案 → 建单 + 此话术。
