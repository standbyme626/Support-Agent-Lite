---
key: reply_clarify
version: 1
applies_to: clarify
not_applies_to: chitchat
requires: -
failure_mode: fallback_main
output_format: markdown, 1-2个问题
---
## summary
澄清追问：信息不足时一次最多问两个关键问题，给出示例引导。
## full
话术模板：
为了更快帮您解决，请补充：1) {q1}（例如：{example1}）2) {q2}。
边界：问题必须与故障定位直接相关；不得连环追问超过两问。
示例：报修无地点 → 问楼层/房间号。
