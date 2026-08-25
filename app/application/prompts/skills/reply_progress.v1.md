---
key: reply_progress
version: 1
applies_to: progress_query
not_applies_to: chitchat, handoff
requires: -
failure_mode: fallback_main
output_format: markdown, ≤3段, 含工单号
---
## summary
进度查询回复：基于 ticket_history 真实事件流如实转述，不预测时间。
## full
话术模板：
工单 {ticket_id} 当前状态 {status}；最近进展：{last_event}。
边界：事件为空 → 如实说明尚未开始处理；不得承诺完成时间；无关联工单 → 请用户提供工单号。
示例：OPEN+已创建 → 「已受理，等待运维认领」。
